import asyncio
import functools
import logging
import re
from datetime import datetime
from typing import Dict, List, Optional, Union

import discord
import tweepy
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.i18n import Translator
from redbot.core.utils import bounded_gather
from tweepy.asynchronous import AsyncStreamingClient

from .tweet_entry import TweetEntry

_ = Translator("Tweets", __file__)

log = logging.getLogger("red.trusty-cogs.Tweets")

USER_FIELDS = [
    "created_at",
    "description",
    "entities",
    "public_metrics",
    "profile_image_url",
    "location",
    "pinned_tweet_id",
    "protected",
    "url",
    "verified",
]
TWEET_FIELDS = [
    "attachments",
    "author_id",
    "created_at",
    "entities",
    "in_reply_to_user_id",
    "lang",
    "public_metrics",
    "possibly_sensitive",
    "referenced_tweets",
]
EXPANSIONS = [
    "author_id",
    "referenced_tweets.id",
    "entities.mentions.username",
    "referenced_tweets.id.author_id",
    "attachments.media_keys",
]
MEDIA_FIELDS = [
    "duration_ms",
    "height",
    "media_key",
    "preview_image_url",
    "type",
    "url",
    "width",
    "alt_text",
]

SCOPES = [
    "tweet.read",
    "tweet.write",
    "users.read",
    "follows.read",
    "follows.write",
    "offline.access",
    "like.read",
    "like.write",
]


class MissingTokenError(Exception):
    async def send_error(self, ctx: commands.Context):
        await ctx.send(
            _(
                "You need to set your API tokens. See `{prefix}tweetset creds` for information on how."
            ).format(prefix=ctx.clean_prefix)
        )


async def get_tweet_text(tweet: tweepy.Tweet) -> str:
    if not tweet.entities:
        return tweet.text
    text = tweet.text
    for url in tweet.entities.get("urls", []):
        display_url = url["display_url"]
        expanded_url = url["expanded_url"]
        full_url = f"[{display_url}]({expanded_url})"
        text = text.replace(url["url"], full_url)
    for mention in tweet.entities.get("mentions", []):
        username = mention.get("username", None)
        user_mention = f"@{username}"
        url = f"[{user_mention}](https://twitter.com/{username})"
        text = re.sub(rf"@{username}\b", url, text)
    return text


class TweetListener(AsyncStreamingClient):
    def __init__(
        self,
        bearer_token: str,
        bot: Red,
    ):
        super().__init__(
            bearer_token=bearer_token,
            wait_on_rate_limit=True,
        )
        self.bot = bot
        self.is_rate_limited = False

    async def on_response(self, response: tweepy.StreamResponse) -> None:
        self.bot.dispatch("tweet", response)

    async def on_errors(self, errors: dict) -> None:
        msg = _("A tweet stream error has occured! ") + str(errors)
        log.error(msg)
        self.bot.dispatch("tweet_error", msg)

    async def on_exception(self, exception: Exception):
        msg = _("A tweet stream error has occured! ") + str(exception)
        log.exception(msg)
        self.bot.dispatch("tweet_error", msg)

    async def on_request_error(self, status_code):
        msg = _("The twitter stream encounterd an error code {code}").format(code=status_code)
        log.debug(msg)
        if status_code == 429:
            self.is_rate_limited = True
            self.disconnect()
            await asyncio.sleep(60 * 16)
            self.is_rate_limited = False

    async def on_disconnect(self) -> None:
        log.info(_("The stream has disconnected."))


class TweetsAPI:
    """
    Here is all the logic for handling autotweets
    """

    config: Config
    bot: Red
    accounts: Dict[str, TweetEntry]
    run_stream: bool
    twitter_loop: Optional[tweepy.Stream]
    tweet_stream_view: discord.ui.View

    async def start_stream(self) -> None:
        await self.bot.wait_until_red_ready()
        base_sleep = 300
        count = 1
        while self.run_stream:
            tokens = await self.bot.get_shared_api_tokens("twitter")
            if not tokens:
                # Don't run the loop until tokens are set
                await asyncio.sleep(base_sleep)
                continue
            # if not api:
            # api = await self.authenticate()
            bearer_token = tokens.get("bearer_token", None)
            if bearer_token is None:
                await asyncio.sleep(base_sleep)
                continue
            self.mystream = TweetListener(bearer_token=bearer_token, bot=self.bot)
            if self.mystream.task is None:
                await self._start_stream()
            if self.mystream.task:
                if (
                    self.mystream.task.cancelled() or self.mystream.task.done()
                ) and not self.mystream.is_rate_limited:
                    count += 1
                    await self._start_stream()
            log.debug(f"tweets waiting {base_sleep * count} seconds.")
            await asyncio.sleep(base_sleep * count)

    async def _start_stream(self) -> None:
        try:
            self.mystream.filter(
                expansions=EXPANSIONS,
                media_fields=MEDIA_FIELDS,
                tweet_fields=TWEET_FIELDS,
                user_fields=USER_FIELDS,
            )
        except Exception:
            log.exception("Error starting stream")

    async def refresh_token(self, user: discord.abc.User) -> dict:
        tokens = await self.bot.get_shared_api_tokens("twitter")
        client_id = tokens.get("client_id")
        redirect_uri = tokens.get("redirect_uri")
        client_secret = tokens.get("client_secret")
        oauth = tweepy.OAuth2UserHandler(
            client_id=client_id,
            redirect_uri=redirect_uri,
            scope=SCOPES,
            client_secret=client_secret,
        )
        user_tokens = await self.config.user(user).tokens()
        refresh_token = user_tokens.get("refresh_token")
        loop = asyncio.get_running_loop()
        task = functools.partial(
            oauth.refresh_token,
            token_url="https://api.twitter.com/2/oauth2/token",
            refresh_token=refresh_token,
        )
        result = await loop.run_in_executor(None, task)
        await self.config.user(user).tokens.set(result)
        return result

    async def authorize_user(
        self,
        ctx: Optional[commands.Context] = None,
        interaction: Optional[discord.Interaction] = None,
    ) -> bool:
        if ctx is None and interaction is None:
            return False
        if ctx is None:
            user = interaction.user
        if interaction is None:
            user = ctx.author
        tokens = await self.bot.get_shared_api_tokens("twitter")
        client_id = tokens.get("client_id")
        redirect_uri = tokens.get("redirect_uri")
        client_secret = tokens.get("client_secret")
        oauth = tweepy.OAuth2UserHandler(
            client_id=client_id,
            redirect_uri=redirect_uri,
            scope=SCOPES,
            client_secret=client_secret,
        )
        user_tokens = await self.config.user(user).tokens()
        if user_tokens:
            if user_tokens.get("expires_at", 0) <= datetime.now().timestamp():
                return True
            else:
                await self.refresh_token(user)
                return True
        oauth_url = oauth.get_authorization_url()
        if interaction is not None or ctx and ctx.interaction:
            msg = _(
                "Please accept the authorization [here]({auth}) and **DM "
                "me** with the final full url."
            ).format(auth=oauth_url)

        else:
            msg = _(
                "Please accept the authorization in the following link and reply "
                "to me with the full url\n\n {auth}"
            ).format(auth=oauth_url)

        def check(message):
            return (user.id in self.dashboard_authed) or (
                message.author.id == user.id and redirect_uri in message.content
            )

        if ctx and ctx.interaction:
            await ctx.send(msg, ephemeral=True)
        elif interaction is not None:
            await interaction.response.send_message(msg, ephemeral=True)
        else:
            try:
                await user.send(msg)
            except discord.errors.Forbidden:
                await ctx.send(msg)
        try:
            check_msg = await self.bot.wait_for("message", check=check, timeout=180)
        except asyncio.TimeoutError:
            await ctx.send(_("Alright I won't interact with twitter for you."))
            return False
        final_url = check_msg.clean_content.strip()
        loop = asyncio.get_running_loop()
        try:
            task = functools.partial(oauth.fetch_token, authorization_response=final_url)
            result = await loop.run_in_executor(None, task)
        except Exception:
            log.exception("Error authorizing user via OAuth.")
            return False
        await self.config.user(user).tokens.set(result)
        return True

    async def authenticate(self, user: Optional[discord.abc.User] = None) -> tweepy.API:
        """Authenticate with Twitter's API"""
        token_kwargs = {}
        if user is None:
            keys = await self.bot.get_shared_api_tokens("twitter")
            token_kwargs["bearer_token"] = keys.get("bearer_token")
            if any([k is None for k in token_kwargs.values()]):
                raise MissingTokenError(
                    "One or more of the required API tokens is missing for the cog to work."
                )
        else:
            user_tokens = await self.config.user(user).tokens()
            if not user_tokens:
                raise MissingTokenError()
            if datetime.now().timestamp() >= user_tokens.get("expires_at", 0):
                user_tokens = await self.refresh_token(user)
            token_kwargs["bearer_token"] = user_tokens.get("access_token")
        # auth = tweepy.OAuthHandler(consumer, consumer_secret)
        # auth.set_access_token(access_token, access_secret)
        return tweepy.asynchronous.AsyncClient(
            **token_kwargs,
            wait_on_rate_limit=True,
        )

    async def autotweet_restart(self) -> None:
        if self.mystream is not None:
            self.mystream.disconnect()
        self.twitter_loop.cancel()
        self.twitter_loop = asyncio.create_task(self.start_stream())

    @commands.Cog.listener()
    async def on_tweet_error(self, error: str) -> None:
        """Posts tweet stream errors to a specified channel"""
        help_msg = _(
            "\n See here for more information "
            "<https://developer.twitter.com/en/support/twitter-api/error-troubleshooting>"
        )
        if "420" in error:
            help_msg += _(
                "You're being rate limited. Maybe you should unload the cog for a while..."
            )
            log.critical(str(error) + help_msg)
        guild_id = await self.config.error_guild()
        channel_id = await self.config.error_channel()

        if guild_id is None and channel_id is not None:
            channel = self.bot.get_channel(channel_id)
            if not channel:
                return
            guild_id = channel.guild.id
            await self.config.error_guild.set(channel.guild.id)

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        channel = guild.get_channel(channel_id)
        if channel is None:
            return
        if not channel.permissions_for(guild.me).send_messages:
            return
        await channel.send(str(error) + help_msg)

    async def get_user(self, user_id: int, includes: Optional[dict] = None) -> tweepy.User:
        if includes:
            for user in includes.get("users", []):
                if user.id == user_id:
                    return user
        api = await self.authenticate()
        resp = await api.get_user(id=user_id, user_fields=USER_FIELDS)
        return resp.data

    async def get_tweet(self, tweet_id: int, includes: Optional[dict] = None) -> tweepy.Tweet:
        if includes:
            for tweet in includes.get("tweets", []):
                if tweet_id == tweet.id:
                    return tweet
        api = await self.authenticate()
        resp = await api.get_tweet(id=tweet_id, tweet_fields=TWEET_FIELDS)
        return resp.data

    async def get_media_url(self, media_key: str, includes: dict) -> Optional[tweepy.Media]:
        if includes:
            for media in includes.get("media", []):
                if media_key == media.media_key:
                    return media
        return None

    async def build_tweet_embed(
        self, response: tweepy.StreamResponse
    ) -> Dict[str, Union[List[discord.Embed], str]]:
        embeds = []
        tweet = response.data
        includes = response.includes

        user_id = tweet.author_id
        author = await self.get_user(user_id, includes)
        username = author.username
        post_url = "https://twitter.com/{}/status/{}".format(username, tweet.id)
        em = discord.Embed(
            url=post_url,
            timestamp=tweet.created_at,
        )
        em.set_footer(text=f"@{username}")
        em.set_author(name=author.name, url=post_url, icon_url=author.profile_image_url)
        em.description = await get_tweet_text(tweet)
        attachment_keys = []
        nsfw = tweet.possibly_sensitive
        if tweet.attachments:
            for media_key in tweet.attachments.get("media_keys", []):
                attachment_keys.append(media_key)
        if tweet.referenced_tweets:
            for replied_tweet in tweet.referenced_tweets:
                replied_to = await self.get_tweet(replied_tweet["id"], includes)
                if replied_to is None:
                    continue
                replied_user = await self.get_user(replied_to.author_id, includes)
                if replied_user is None:
                    continue
                if replied_to.attachments:
                    for media_key in replied_to.attachments.get("media_keys", []):
                        attachment_keys.append(media_key)
                name = _("Replying to {user}").format(user=replied_user.username)
                if tweet.text.startswith("RT"):
                    name = _("Retweeted {user}").format(user=replied_user.username)
                em.add_field(
                    name=name,
                    value=await get_tweet_text(replied_to),
                    inline=False,
                )
                nsfw |= replied_to.possibly_sensitive
        if attachment_keys:
            for media_key in attachment_keys:
                copy = em.copy()
                media = await self.get_media_url(media_key, includes)
                if media is None:
                    continue
                url = media.url
                if url is None:
                    url = media.preview_image_url
                copy.set_image(url=url)
                embeds.append(copy)

        if not embeds:
            embeds.append(em)
        return {"embeds": embeds[:10], "content": str(post_url), "nsfw": nsfw}

    @commands.Cog.listener()
    async def on_tweet(self, response: tweepy.StreamResponse) -> None:
        log.info(response)
        try:
            tweet = response.data
            user = await self.get_user(tweet.author_id, response.includes)
            to_send = await self.build_tweet_embed(response)
            all_channels = await self.config.all_channels()
            tasks = []
            nsfw = to_send.pop("nsfw", False)
            for channel_id, data in all_channels.items():
                guild = self.bot.get_guild(data.get("guild_id", ""))
                if guild is None:
                    continue
                channel = guild.get_channel(int(channel_id))
                if channel is None:
                    continue
                if nsfw and not channel.is_nsfw():
                    log.info(f"Ignoring tweet from {user} because it is labeled as NSFW.")
                    continue
                if str(user.id) in data.get("followed_accounts", {}):
                    tasks.append(
                        self.post_tweet_status(
                            channel, to_send["embeds"], to_send["content"], tweet, user
                        )
                    )
                    continue
                for rule in response.matching_rules:
                    if rule.tag in data.get("followed_rules", {}):
                        tasks.append(
                            self.post_tweet_status(
                                channel, to_send["embeds"], to_send["content"], tweet, user
                            )
                        )
                        continue
                for phrase in data.get("followed_str", {}):
                    if phrase in tweet.text:
                        tasks.append(
                            self.post_tweet_status(
                                channel, to_send["embeds"], to_send["content"], tweet, user
                            )
                        )
                        continue
            await bounded_gather(*tasks, return_exceptions=True)
        except Exception:
            log.exception("Error on tweet status")

    async def post_tweet_status(
        self,
        channel: discord.TextChannel,
        embeds: List[discord.Embed],
        content: str,
        tweet: tweepy.Tweet,
        user: tweepy.User,
    ):
        if await self.bot.cog_disabled_in_guild(self, channel.guild):
            return
        view = None
        if await self.config.channel(channel).add_buttons():
            view = self.tweet_stream_view
        try:
            if channel.permissions_for(channel.guild.me).manage_webhooks:
                webhook = None
                for hook in await channel.webhooks():
                    if hook.name == channel.guild.me.name:
                        webhook = hook
                if webhook is None:
                    webhook = await channel.create_webhook(name=channel.guild.me.name)
                await webhook.send(
                    content,
                    username=user.username,
                    avatar_url=user.profile_image_url,
                    embeds=embeds,
                    view=view,
                )
            elif channel.permissions_for(channel.guild.me).embed_links:
                await channel.send(content, embeds=embeds, view=view)
            else:
                await channel.send(content, view=view)
        except Exception:
            log.exception(f"Could not post a tweet in {repr(channel)} for account {user}")
