import copy
import datetime
import json
import logging
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from permissions import is_owner
import command_perms
import database as db
import guild_config as gc

# Fallback defaults for any server that hasn't customized its scam-word list yet - keyed
# the same way applications.py handles DEFAULT_APPLICATIONS, for the same reason (a server's
# real list lives in the generic settings table under "antiraid_scam_keywords" as JSON).
DEFAULT_SCAM_KEYWORDS = [
    "free nitro",
    "steam gift",
    "steamcommunity.ru",
    "discocrd",
    "discrod",
    "dlscord",
    "@everyone free",
    "airdrop claim",
    "nitro giveaway free",
]

DEFAULT_SPAM_LIMIT = 5          # a user may repeat the same message this many times before being caught
DEFAULT_SPAM_WINDOW = 30        # ...as long as the repeats land within this many seconds of each other
DEFAULT_MUTE_MINUTES = 5


def is_admin_perm(member: discord.Member) -> bool:
    if is_owner(member.id):
        return True
    if command_perms.granted():
        return True
    return member.guild_permissions.administrator


def get_keywords(guild_id: int) -> list:
    """See DEFAULT_SCAM_KEYWORDS docstring above - always returns a fresh list, never the
    live default, so one server customizing its list can't corrupt every other server's
    fallback for the life of the process."""
    raw = db.get_setting(guild_id, "antiraid_scam_keywords")
    if raw:
        try:
            data = json.loads(raw)
            if data:
                return data
        except (json.JSONDecodeError, TypeError):
            pass
    return copy.deepcopy(DEFAULT_SCAM_KEYWORDS)


def save_keywords(guild_id: int, keywords: list):
    db.set_setting(guild_id, "antiraid_scam_keywords", json.dumps(keywords))


def contains_scam_pattern(content: str, keywords: list) -> Optional[str]:
    lowered = content.lower()
    for word in keywords:
        if word.lower() in lowered:
            return word
    return None


class AntiRaid(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # guild_id -> user_id -> {"content": str, "count": int, "first_ts": float, "messages": [Message]}
        # In-memory only, like Applications.active (see the
        # README) - a restart mid-burst just means the burst counter resets, nothing is lost.
        self._recent: dict[int, dict[int, dict]] = {}

    # ---------------- settings helpers ----------------

    def _is_enabled(self, guild_id: int) -> bool:
        return db.get_setting(guild_id, "antiraid_enabled", "1") == "1"

    def _spam_limit(self, guild_id: int) -> int:
        try:
            return int(db.get_setting(guild_id, "antiraid_spam_limit", str(DEFAULT_SPAM_LIMIT)))
        except (TypeError, ValueError):
            return DEFAULT_SPAM_LIMIT

    def _spam_window(self, guild_id: int) -> int:
        try:
            return int(db.get_setting(guild_id, "antiraid_spam_window", str(DEFAULT_SPAM_WINDOW)))
        except (TypeError, ValueError):
            return DEFAULT_SPAM_WINDOW

    def _mute_minutes(self, guild_id: int) -> int:
        try:
            return int(db.get_setting(guild_id, "antiraid_mute_minutes", str(DEFAULT_MUTE_MINUTES)))
        except (TypeError, ValueError):
            return DEFAULT_MUTE_MINUTES

    def _is_immune(self, member: discord.Member) -> bool:
        if is_owner(member.id):
            return True
        if member.guild.owner_id == member.id:
            return True
        if member.guild_permissions.administrator or member.guild_permissions.manage_messages:
            return True
        role_ids = {r.id for r in member.roles}
        return bool(role_ids & {gc.get(member.guild.id, "staff-role"), gc.get(member.guild.id, "admin-role")})

    async def _log(self, guild: discord.Guild, embed: discord.Embed):
        # Never let a missing/locked log channel break the catch itself.
        channel = gc.get_log_channel(guild, "antiraid-log-channel")
        if channel is None:
            return
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            pass

    # ---------------- the detector ----------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        if not self._is_enabled(message.guild.id):
            return
        if not isinstance(message.author, discord.Member) or self._is_immune(message.author):
            return

        # Scam pattern check first - more severe, no need to also run it through the spam
        # tracker below.
        try:
            keywords = get_keywords(message.guild.id)
            hit = contains_scam_pattern(message.content or "", keywords)
            if hit:
                await self._handle_scam(message, hit)
                return

            await self._track_spam(message)
        except Exception:
            logging.exception("antiraid: error while checking a message")

    async def _handle_scam(self, message: discord.Message, matched: str):
        member = message.author
        guild = message.guild
        caught_content = (message.content or "*(no text)*")[:1000]

        try:
            await message.delete()
        except discord.HTTPException:
            pass

        action_taken = "None (failed)"
        try:
            await member.kick(reason=f"Anti-raid: message matched scam pattern '{matched}'")
            action_taken = "Kicked"
        except discord.Forbidden:
            action_taken = "Failed — missing permissions"
        except discord.HTTPException as e:
            action_taken = f"Failed — {e}"

        db.add_infraction(guild.id, member.id, "antiraid-scam",
                           f"Posted a scam-pattern message in #{message.channel.name} (matched: {matched})",
                           self.bot.user.id)

        embed = discord.Embed(
            title="🚨 Anti-Raid: Scam Message Caught",
            description=f"{member.mention} posted a suspected scam message in {message.channel.mention} and was **{action_taken}**.",
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="User", value=f"{member} (`{member.id}`)", inline=False)
        embed.add_field(name="Matched Pattern", value=matched, inline=True)
        embed.add_field(name="Caught Message", value=caught_content[:1024], inline=False)
        await self._log(guild, embed)

    async def _track_spam(self, message: discord.Message):
        guild_id = message.guild.id
        member = message.author
        content = (message.content or "").strip().lower()
        if not content:
            return  # attachment-only / embed-only messages aren't tracked as text spam

        now = time.time()
        window = self._spam_window(guild_id)
        limit = self._spam_limit(guild_id)

        guild_tracker = self._recent.setdefault(guild_id, {})

        # Drop expired entries so this dict (and the Message objects it holds) can't grow forever
        # with users who sent a couple of messages once and never tripped anything.
        if len(guild_tracker) > 50:
            for uid in [u for u, e in guild_tracker.items() if now - e["first_ts"] > window]:
                guild_tracker.pop(uid, None)

        entry = guild_tracker.get(member.id)

        if entry and entry["content"] == content and (now - entry["first_ts"]) <= window:
            entry["count"] += 1
            entry["messages"].append(message)
        else:
            entry = {"content": content, "count": 1, "first_ts": now, "messages": [message]}
            guild_tracker[member.id] = entry

        if entry["count"] > limit:
            # Remove first: _handle_spam awaits (deleting messages, timing out), and more messages
            # arriving meanwhile must not re-trigger the same catch.
            guild_tracker.pop(member.id, None)
            await self._handle_spam(message.guild, member, entry, limit)

    async def _handle_spam(self, guild: discord.Guild, member: discord.Member, entry: dict, limit: int):
        mute_minutes = self._mute_minutes(guild.id)

        for msg in entry["messages"]:
            try:
                await msg.delete()
            except discord.HTTPException:
                continue

        action_taken = "None (failed)"
        try:
            until = discord.utils.utcnow() + datetime.timedelta(minutes=mute_minutes)
            await member.timeout(until, reason=f"Anti-raid: repeated the same message {entry['count']} times")
            action_taken = f"Muted for {mute_minutes} minute(s)"
        except discord.Forbidden:
            action_taken = "Failed — missing permissions"
        except discord.HTTPException as e:
            action_taken = f"Failed — {e}"

        db.add_infraction(guild.id, member.id, "antiraid-spam",
                           f"Repeated the same message {entry['count']} times", self.bot.user.id)

        embed = discord.Embed(
            title="🚨 Anti-Raid: Spam Caught",
            description=f"{member.mention} repeated the same message **{entry['count']}** times and was **{action_taken}**.",
            color=discord.Color.orange(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="User", value=f"{member} (`{member.id}`)", inline=False)
        embed.add_field(name="Messages Deleted", value=str(len(entry["messages"])), inline=True)
        embed.add_field(name="Repeated Content", value=entry["content"][:1024] or "*(empty)*", inline=False)
        await self._log(guild, embed)

    # ---------------- /antiraid ----------------

    antiraid_group = app_commands.Group(name="antiraid", description="[Admin] Configure anti-raid spam & scam detection")

    @antiraid_group.command(name="config", description="Configure anti-raid settings (all in one place)")
    @app_commands.describe(
        enabled="Turn anti-raid detection on or off",
        spam_limit="How many times a user can repeat the same message before getting muted",
        spam_window_seconds="How many seconds the repeats must land within to count as a burst",
        mute_minutes="How long the mute lasts when someone trips the spam detector",
        log_channel="Where catches get logged",
    )
    @app_commands.choices(enabled=[
        app_commands.Choice(name="On", value="on"),
        app_commands.Choice(name="Off", value="off"),
    ])
    async def antiraid_config(
        self,
        interaction: discord.Interaction,
        enabled: app_commands.Choice[str] = None,
        spam_limit: app_commands.Range[int, 2, 50] = None,
        spam_window_seconds: app_commands.Range[int, 5, 600] = None,
        mute_minutes: app_commands.Range[int, 1, 10080] = None,
        log_channel: discord.TextChannel = None,
    ):
        if not is_admin_perm(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)

        gid = interaction.guild_id
        changes = []

        if enabled is not None:
            db.set_setting(gid, "antiraid_enabled", "1" if enabled.value == "on" else "0")
            changes.append(f"Anti-raid turned **{enabled.name}**")

        if spam_limit is not None:
            db.set_setting(gid, "antiraid_spam_limit", str(spam_limit))
            changes.append(f"Spam limit set to **{spam_limit}** repeats")

        if spam_window_seconds is not None:
            db.set_setting(gid, "antiraid_spam_window", str(spam_window_seconds))
            changes.append(f"Spam window set to **{spam_window_seconds}s**")

        if mute_minutes is not None:
            db.set_setting(gid, "antiraid_mute_minutes", str(mute_minutes))
            changes.append(f"Mute duration set to **{mute_minutes} minute(s)**")

        if log_channel is not None:
            gc.set_override(gid, "antiraid-log-channel", log_channel.id)
            changes.append(f"Log channel set to {log_channel.mention}")

        if not changes:
            return await interaction.response.send_message(
                "No settings provided. Pass at least one option to change, or use `/antiraid status` to view current config.",
                ephemeral=True,
            )

        await interaction.response.send_message("Updated:\n" + "\n".join(f"- {c}" for c in changes), ephemeral=True)

    @antiraid_group.command(name="status", description="View the current anti-raid configuration")
    async def antiraid_status(self, interaction: discord.Interaction):
        if not is_admin_perm(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)

        gid = interaction.guild_id
        log_channel = gc.get_log_channel(interaction.guild, "antiraid-log-channel")
        own_log = gc.get(gid, "antiraid-log-channel")
        keywords = get_keywords(gid)

        embed = discord.Embed(
            title="🚨 Anti-Raid Configuration",
            color=discord.Color.green() if self._is_enabled(gid) else discord.Color.dark_grey(),
        )
        embed.add_field(name="Status", value="🟢 Enabled" if self._is_enabled(gid) else "🔴 Disabled", inline=True)
        embed.add_field(name="Log Channel", value=(log_channel.mention + ("" if own_log else " *(unified log)*")) if log_channel else "Not set", inline=True)
        embed.add_field(name="Spam Rule", value=f"> {self._spam_limit(gid)} repeats in {self._spam_window(gid)}s → mute {self._mute_minutes(gid)}m", inline=False)
        embed.add_field(name=f"Scam Keywords ({len(keywords)})", value=", ".join(f"`{k}`" for k in keywords)[:1024] or "None set", inline=False)
        embed.set_footer(text="Staff, admins, and the bot owner are always immune.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @antiraid_group.command(name="word-add", description="Add a keyword/phrase that triggers a scam kick")
    @app_commands.describe(word="Text to match (case-insensitive substring match against message content)")
    async def scamword_add(self, interaction: discord.Interaction, word: str):
        if not is_admin_perm(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        keywords = get_keywords(interaction.guild_id)
        if word.lower() in (k.lower() for k in keywords):
            return await interaction.response.send_message(f"`{word}` is already in the list.", ephemeral=True)
        keywords.append(word)
        save_keywords(interaction.guild_id, keywords)
        await interaction.response.send_message(f"✅ Added `{word}` to the scam keyword list.", ephemeral=True)

    @antiraid_group.command(name="word-remove", description="Remove a keyword/phrase from the scam kick list")
    @app_commands.describe(word="The exact keyword/phrase to remove")
    async def scamword_remove(self, interaction: discord.Interaction, word: str):
        if not is_admin_perm(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        keywords = get_keywords(interaction.guild_id)
        matches = [k for k in keywords if k.lower() == word.lower()]
        if not matches:
            return await interaction.response.send_message(f"`{word}` isn't in the list.", ephemeral=True)
        keywords = [k for k in keywords if k.lower() != word.lower()]
        save_keywords(interaction.guild_id, keywords)
        await interaction.response.send_message(f"🗑️ Removed `{word}` from the scam keyword list.", ephemeral=True)

    @antiraid_group.command(name="word-list", description="List the scam keywords that trigger a kick")
    async def scamword_list(self, interaction: discord.Interaction):
        if not is_admin_perm(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        keywords = get_keywords(interaction.guild_id)
        listing = "\n".join(f"**{i}.** `{k}`" for i, k in enumerate(keywords, start=1)) or "No keywords configured."
        embed = discord.Embed(title="🚨 Scam Keywords", description=listing[:4000], color=discord.Color.blurple())
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AntiRaid(bot))
