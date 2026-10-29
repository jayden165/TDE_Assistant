"""
YouTube upload notifications for The Daily Emerald.

    /upload channel set / remove     locked - notifications always go to the one fixed channel (see below)
    /upload ping [role]              role to ping with each notification (leave empty to stop pinging)
    /upload status                   what's set up, when it last checked, any problems
    /upload check                    check YouTube right now

Every few minutes (YOUTUBE_POLL_MINUTES, default 5) the bot checks the channel's RSS feed for new videos and
Shorts, and its Posts tab for new community posts, and posts an embed with a button to ONE place only:
config.YOUTUBE_NOTIFY_CHANNEL_ID in config.YOUTUBE_NOTIFY_GUILD_ID. No other server or channel ever gets
notifications, whatever is saved in the database. The first check only *remembers* what already exists, so old
uploads are never announced. At most 5 items are posted per check. The ping role is still /upload ping.

The commands are registered in cog_load (a nested group: /upload -> channel -> set) and removed on unload.
"""
import asyncio
import json
import logging
import time
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands, tasks

import command_perms
import config
import database as db
import youtube
from permissions import is_owner

MAX_PER_CHECK = 5
SEEN_LIMIT = 300
RED = 0xFF0000


def is_admin_perm(member: discord.Member) -> bool:
    if is_owner(member.id) or command_perms.granted():
        return True
    return member.guild_permissions.administrator


class Uploads(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._lock = asyncio.Lock()
        self.last_check: float | None = None
        self.last_errors: dict[str, str] = {}

    async def cog_load(self):
        self._register_commands()
        self.poll.start()

    async def cog_unload(self):
        self.poll.cancel()
        self.bot.tree.remove_command("upload")

    # ---------------- saved state ----------------

    @staticmethod
    def _seen() -> list[str]:
        try:
            return json.loads(db.get_setting(0, "yt_seen", "[]"))
        except (ValueError, TypeError):
            return []

    @staticmethod
    def _remember(ids: list[str]):
        seen = Uploads._seen()
        seen.extend(i for i in ids if i not in seen)
        db.set_setting(0, "yt_seen", json.dumps(seen[-SEEN_LIMIT:]))

    @staticmethod
    def _seeded(source: str) -> bool:
        return bool(db.get_setting(0, f"yt_seeded_{source}"))

    def _targets(self) -> list[tuple[discord.TextChannel, discord.Role | None]]:
        """The ONE notification channel (and ping role) - fixed in config, never read from per-server settings."""
        guild = self.bot.get_guild(config.YOUTUBE_NOTIFY_GUILD_ID)
        channel = guild.get_channel(config.YOUTUBE_NOTIFY_CHANNEL_ID) if guild else None
        if not isinstance(channel, discord.TextChannel):
            return []
        role_id = db.get_setting(guild.id, "yt_ping_role")
        return [(channel, guild.get_role(int(role_id)) if role_id else None)]

    # ---------------- checking YouTube ----------------

    @tasks.loop(minutes=max(1, config.YOUTUBE_POLL_MINUTES))
    async def poll(self):
        try:
            await self.run_poll()
        except Exception:
            logging.exception("YouTube check failed")

    @poll.before_loop
    async def _wait(self):
        await self.bot.wait_until_ready()

    async def run_poll(self) -> dict:
        """One check. Returns {"posted": n, "seeded": [sources], "errors": {source: why}}."""
        result = {"posted": 0, "seeded": [], "errors": {}}
        async with self._lock:
            targets = self._targets()
            if not targets:
                return result
            async with youtube.new_session() as session:
                for source in ("videos", "posts"):
                    try:
                        if source == "videos":
                            items = await youtube.fetch_videos(session, config.YOUTUBE_CHANNEL_ID)
                        else:
                            items = await youtube.fetch_posts(session, config.YOUTUBE_CHANNEL_ID, config.YOUTUBE_CHANNEL_HANDLE)
                    except Exception as e:
                        logging.warning("YouTube %s check failed: %s", source, e)
                        result["errors"][source] = str(e)[:150]
                        continue
                    seen = set(self._seen())
                    new = [i for i in items if i["id"] not in seen]
                    if not self._seeded(source):
                        # first look: remember what exists, announce nothing (only if we actually saw something)
                        if items:
                            self._remember([i["id"] for i in items])
                            db.set_setting(0, f"yt_seeded_{source}", "1")
                            result["seeded"].append(source)
                        continue
                    new.reverse()  # feeds are newest-first; announce oldest-first
                    for item in new[-MAX_PER_CHECK:]:
                        if source == "videos" and item["kind"] == "video" and await youtube.is_short(session, item["video_id"]):
                            item["kind"] = "short"
                        await self._announce(item, targets)
                        result["posted"] += 1
                    self._remember([i["id"] for i in new])
            self.last_check, self.last_errors = time.time(), result["errors"]
        return result

    # ---------------- posting ----------------

    @staticmethod
    def build_message(item: dict) -> tuple[str, discord.Embed, discord.ui.View]:
        name = config.YOUTUBE_CHANNEL_NAME
        kind = item["kind"]
        heading, button = {
            "video": (f"🎬 **{name}** just uploaded a new video!", "Watch on YouTube"),
            "short": (f"📱 **{name}** just posted a new Short!", "Watch the Short"),
            "post": (f"📝 **{name}** just made a new community post!", "View the post"),
        }[kind]
        text = (item.get("text") or "").strip()
        if kind != "post" and len(text) > 300:
            text = text[:300].rsplit(" ", 1)[0] + "…"
        embed = discord.Embed(title=item["title"][:256] or "New post", url=item["url"], color=RED,
                              description=text[:1500] or None)
        embed.set_author(name=name, url=f"https://www.youtube.com/@{config.YOUTUBE_CHANNEL_HANDLE}")
        if item.get("image"):
            embed.set_image(url=item["image"])
        if kind != "post" and item.get("published"):
            try:
                embed.timestamp = datetime.fromisoformat(item["published"])
            except ValueError:
                pass
        if kind == "post":
            embed.set_footer(text="YouTube community post" + (f" · {item['published']}" if item.get("published") else ""))
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label=button, url=item["url"], emoji="▶️" if kind != "post" else "📝"))
        return heading, embed, view

    async def _announce(self, item: dict, targets: list):
        heading, embed, view = self.build_message(item)
        for channel, role in targets:
            content = f"{role.mention} {heading}" if role else heading
            try:
                await channel.send(content=content, embed=embed, view=view,
                                   allowed_mentions=discord.AllowedMentions(roles=True, everyone=False, users=False))
            except discord.HTTPException:
                logging.warning("couldn't post a YouTube notification in #%s (%s)", channel.name, channel.guild.name)

    # ---------------- commands ----------------

    def _register_commands(self):
        cog = self

        async def deny(interaction: discord.Interaction):
            await interaction.response.send_message(command_perms.NO_PERMS_MESSAGE, ephemeral=True)

        def private(interaction: discord.Interaction) -> bool:
            return not getattr(interaction, "is_prefix", False)

        def locked_text() -> str:
            return (f"Upload notifications are locked to <#{config.YOUTUBE_NOTIFY_CHANNEL_ID}> "
                    f"(server `{config.YOUTUBE_NOTIFY_GUILD_ID}`) and can't be moved or turned on anywhere else.")

        async def channel_set(interaction: discord.Interaction, channel: discord.TextChannel = None):
            if not is_admin_perm(interaction.user):
                return await deny(interaction)
            await interaction.response.send_message(locked_text(), ephemeral=private(interaction))

        async def channel_remove(interaction: discord.Interaction):
            if not is_admin_perm(interaction.user):
                return await deny(interaction)
            await interaction.response.send_message(locked_text(), ephemeral=private(interaction))

        @app_commands.describe(role="Role to ping with each notification (leave empty to stop pinging)")
        async def ping(interaction: discord.Interaction, role: discord.Role = None):
            if not is_admin_perm(interaction.user):
                return await deny(interaction)
            if interaction.guild.id != config.YOUTUBE_NOTIFY_GUILD_ID:
                return await interaction.response.send_message("Upload notifications aren't sent in this server.", ephemeral=True)
            if role is None:
                db.delete_setting(interaction.guild.id, "yt_ping_role")
                return await interaction.response.send_message("🔕 Notifications won't ping anyone.", ephemeral=private(interaction))
            db.set_setting(interaction.guild.id, "yt_ping_role", role.id)
            await interaction.response.send_message(f"🔔 Notifications will ping {role.mention}.", ephemeral=private(interaction),
                                                    allowed_mentions=discord.AllowedMentions.none())

        async def status(interaction: discord.Interaction):
            if not is_admin_perm(interaction.user):
                return await deny(interaction)
            guild = interaction.guild
            role_id = db.get_setting(guild.id, "yt_ping_role")
            channel = guild.get_channel(config.YOUTUBE_NOTIFY_CHANNEL_ID) if guild.id == config.YOUTUBE_NOTIFY_GUILD_ID else None
            role = guild.get_role(int(role_id)) if role_id else None
            embed = discord.Embed(title="📺 Upload notifications", color=discord.Color.blurple())
            embed.add_field(name="Watching", value=f"[{config.YOUTUBE_CHANNEL_NAME}](https://www.youtube.com/@{config.YOUTUBE_CHANNEL_HANDLE})\nVideos · Shorts · community posts", inline=False)
            embed.add_field(name="Posts in", value=channel.mention if channel else "⚪ not posting in this server", inline=True)
            embed.add_field(name="Pings", value=role.mention if role else "nobody", inline=True)
            embed.add_field(name="Checks every", value=f"{max(1, config.YOUTUBE_POLL_MINUTES)} min", inline=True)
            embed.add_field(name="Last check", value=f"<t:{int(cog.last_check)}:R>" if cog.last_check else "not yet", inline=True)
            embed.add_field(name="Remembered", value=f"{len(cog._seen())} items", inline=True)
            if cog.last_errors:
                embed.add_field(name="⚠️ Problems", value="\n".join(f"**{k}**: {v}" for k, v in cog.last_errors.items())[:1024], inline=False)
            await interaction.response.send_message(embed=embed, ephemeral=private(interaction), allowed_mentions=discord.AllowedMentions.none())

        async def check(interaction: discord.Interaction):
            if not is_admin_perm(interaction.user):
                return await deny(interaction)
            await interaction.response.defer(ephemeral=private(interaction))
            result = await cog.run_poll()
            if not cog._targets():
                text = "Nothing to check - I can't see the notification channel (check the bot is in the server and can post there)."
            else:
                parts = [f"Posted **{result['posted']}** new item(s)."]
                if result["seeded"]:
                    parts.append(f"First look at {', '.join(result['seeded'])}: remembered what's already there (not announced).")
                if result["errors"]:
                    parts.append("⚠️ " + "; ".join(f"{k}: {v}" for k, v in result["errors"].items()))
                text = " ".join(parts)
            await interaction.followup.send(text, ephemeral=private(interaction))

        upload = app_commands.Group(name="upload", description="[Admin] YouTube upload notifications")
        channel_group = app_commands.Group(name="channel", description="Where upload notifications are posted")
        channel_group.add_command(app_commands.Command(name="set", description="Set the channel for upload notifications", callback=channel_set))
        channel_group.add_command(app_commands.Command(name="remove", description="Stop upload notifications in this server", callback=channel_remove))
        upload.add_command(channel_group)
        upload.add_command(app_commands.Command(name="ping", description="Choose a role to ping with each notification", callback=ping))
        upload.add_command(app_commands.Command(name="status", description="Show how upload notifications are set up", callback=status))
        upload.add_command(app_commands.Command(name="check", description="Check YouTube for new uploads right now", callback=check))
        self.bot.tree.remove_command("upload")
        self.bot.tree.add_command(upload)


async def setup(bot: commands.Bot):
    await bot.add_cog(Uploads(bot))
