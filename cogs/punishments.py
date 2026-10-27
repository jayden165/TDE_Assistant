"""
Moderation commands: ban, unban, kick, timeout, mute, warn.

They are normal slash commands, so the text-prefix system in cogs/prefixcmds.py runs them too:

    s! ban 123456789012345678 advertising in DMs
    s! kick @someone being rude
    s! timeout 123456789012345678 1h spamming        (10m, 2h, 1d, 1h30m - a bare number = minutes)
    s! warn 123456789012345678 last warning about links
    s! timeout 123456789012345678 off                (ends a timeout)
    s! mute 123456789012345678 spam                  (run it again to unmute)
    s! unban 123456789012345678 appealed

The user can be a mention or a raw user ID. Everything after the user (and the duration, for
timeout) is the reason. Bans and unbans also work on people who aren't in the server.

Who can use them (owners always can; /commandperms can open a command to specific roles):
    ban / unban            -> Ban Members
    kick                   -> Kick Members
    timeout / warn / mute -> Moderate Members
Moderators can't act on themselves, the server owner, bot owners, the bot, or anyone whose top role
is equal to or above their own. Actions are saved to /infractions; bans, kicks, timeouts and mutes
also show up in the mod log through cogs/modlog.py, and warnings are posted there directly.

Every reply is an embed (green/orange/red for results, red "Couldn't do that" for refusals and
errors), posted as a reply to the message for prefix commands. Every use of every command - success,
refusal or error - is also written to the Staff Action Log channel (falls back to the unified log
channel) and to the bot's console log.
"""
import datetime
import logging
import re

import discord
from discord import app_commands
from discord.ext import commands

import command_perms
import database as db
import guild_config as gc
from permissions import is_owner

MAX_TIMEOUT = datetime.timedelta(days=28)  # Discord's limit
NO_REASON = "No reason given"

# command name -> the Discord permission that lets someone use it by default
REMOVE_WORDS = {"off", "remove", "end", "none", "cancel", "clear"}  # "timeout <user> off" ends a timeout

REQUIRED_PERMISSION = {
    "ban": "ban_members",
    "unban": "ban_members",
    "kick": "kick_members",
    "timeout": "moderate_members",
    "warn": "moderate_members",
    "mute": "moderate_members",
}

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_DURATION_RE = re.compile(r"(\d+)\s*([smhdw]?)")


def parse_timeout(text: str) -> datetime.timedelta | None:
    """'10m', '2h', '1d', '1h30m', or a bare number of minutes. None if it isn't a duration."""
    text = (text or "").strip().lower()
    if not text or not re.fullmatch(r"(?:\d+\s*[smhdw]?\s*)+", text):
        return None
    seconds = sum(int(n) * _UNITS.get(u or "m", 60) for n, u in _DURATION_RE.findall(text))
    return datetime.timedelta(seconds=seconds) if seconds > 0 else None


def human_duration(delta: datetime.timedelta) -> str:
    seconds = int(delta.total_seconds())
    parts = []
    for label, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if seconds >= size:
            parts.append(f"{seconds // size}{label}")
            seconds %= size
    return " ".join(parts) or "0s"


def can_use(member: discord.Member, command: str) -> bool:
    if is_owner(member.id) or command_perms.granted():
        return True
    return getattr(member.guild_permissions, REQUIRED_PERMISSION[command], False) or \
        member.guild_permissions.administrator


class Punishments(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---------------- helpers ----------------

    # ---------------- replying + logging ----------------

    @staticmethod
    def _via(interaction) -> str:
        if getattr(interaction, "is_prefix", False):
            return f"Prefix: `{interaction.message.content[:200]}`"
        return "Slash command"

    async def _log(self, interaction, command: str, ok: bool, target=None, reason: str | None = None,
                   detail: str | None = None):
        """Records one use of a command in the console log and the staff action log channel.
        Never raises - a logging problem must not break the command itself."""
        who = f"{interaction.user} ({interaction.user.id})"
        logging.info("mod command /%s by %s target=%s ok=%s reason=%r detail=%r", command, who,
                     getattr(target, "id", None), ok, reason, detail)
        try:
            channel = gc.get_log_channel(interaction.guild, "staff-action-log-channel")
            if channel is None:
                return
            embed = discord.Embed(
                title=f"🛡️ /{command} - {'used' if ok else 'failed'}",
                color=discord.Color.orange() if ok else discord.Color.red(),
                timestamp=discord.utils.utcnow(),
            )
            embed.add_field(name="Moderator", value=f"{interaction.user.mention} ({interaction.user.id})", inline=True)
            if target is not None:
                embed.add_field(name="Target", value=f"{target.mention} ({target.id})", inline=True)
            embed.add_field(name="Channel", value=interaction.channel.mention, inline=True)
            embed.add_field(name="Via", value=self._via(interaction), inline=False)
            if reason:
                embed.add_field(name="Reason", value=reason[:1024], inline=False)
            if detail:
                embed.add_field(name="Outcome" if ok else "Why it failed", value=detail[:1024], inline=False)
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            logging.exception("couldn't write the staff action log")

    async def _fail(self, interaction, command: str, message: str, target=None, reason: str | None = None):
        """Replies with a red embed (private for slash commands, a normal reply for prefix ones) and logs it."""
        embed = discord.Embed(title="❌ Couldn't do that", description=message, color=discord.Color.red())
        await interaction.response.send_message(embed=embed, ephemeral=not getattr(interaction, "is_prefix", False))
        await self._log(interaction, command, False, target, reason, message)

    async def _done(self, interaction, command: str, embed: discord.Embed, target, reason: str,
                    detail: str | None = None):
        """Replies with the result embed and logs it."""
        await interaction.response.send_message(embed=embed)
        await self._log(interaction, command, True, target, reason, detail)

    @staticmethod
    def _audit_reason(interaction, reason: str) -> str:
        return f"{interaction.user} ({interaction.user.id}): {reason}"[:512]

    @staticmethod
    def _target_problem(interaction, target) -> str | None:
        """Why this person can't be moderated by the invoker, or None if it's fine.
        `target` may be a Member or a plain User (someone who isn't in the server)."""
        guild, me, invoker = interaction.guild, interaction.guild.me, interaction.user
        if target.id == invoker.id:
            return "You can't do that to yourself."
        if target.id == interaction.client.user.id:
            return "I'm not going to do that to myself."
        if is_owner(target.id) and not is_owner(invoker.id):
            return "You can't moderate a bot owner."
        if isinstance(target, discord.Member):
            if target.id == guild.owner_id:
                return "You can't moderate the server owner."
            if not is_owner(invoker.id) and invoker.id != guild.owner_id \
                    and target.top_role >= invoker.top_role:
                return "That member's top role is the same as or higher than yours."
            if target.top_role >= me.top_role:
                return "That member's top role is the same as or higher than mine - move my role above theirs."
        return None

    @staticmethod
    async def _dm(target, guild: discord.Guild, action: str, reason: str, extra: str = "") -> bool:
        """Best-effort DM so the person knows what happened. True if it was delivered."""
        try:
            embed = discord.Embed(title=f"You were {action} in {guild.name}", color=discord.Color.red(),
                                  timestamp=discord.utils.utcnow())
            if extra:
                embed.add_field(name="Duration", value=extra, inline=False)
            embed.add_field(name="Reason", value=reason[:1024], inline=False)
            await target.send(embed=embed)
            return True
        except (discord.HTTPException, AttributeError):
            return False

    @staticmethod
    def _result(title: str, color: discord.Color, target, interaction, reason: str,
                extra: list | None = None, dm_sent: bool | None = None) -> discord.Embed:
        embed = discord.Embed(title=title, description=f"{target.mention} (`{target.id}`)", color=color,
                              timestamp=discord.utils.utcnow())
        embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
        for name, value in (extra or []):
            embed.add_field(name=name, value=value, inline=True)
        embed.add_field(name="Reason", value=reason[:1024], inline=False)
        if dm_sent is not None:
            embed.set_footer(text="User was notified by DM" if dm_sent else "Couldn't DM the user")
        return embed

    def _member(self, interaction, user) -> discord.Member | None:
        if isinstance(user, discord.Member):
            return user
        return interaction.guild.get_member(user.id)

    # ---------------- /ban ----------------

    @app_commands.command(name="ban", description="[Mod] Ban a member or user ID")
    @app_commands.describe(user="Mention or user ID (works for people who already left)",
                           reason="Why they're being banned")
    async def ban(self, interaction: discord.Interaction, user: discord.User, reason: str = NO_REASON):
        cmd = "ban"
        if not can_use(interaction.user, cmd):
            return await self._fail(interaction, cmd, command_perms.NO_PERMS_MESSAGE, user, reason)
        member = self._member(interaction, user)
        problem = self._target_problem(interaction, member or user)
        if problem:
            return await self._fail(interaction, cmd, problem, user, reason)

        dm_sent = await self._dm(member, interaction.guild, "banned", reason) if member else None
        try:
            await interaction.guild.ban(user, reason=self._audit_reason(interaction, reason), delete_message_seconds=0)
        except discord.Forbidden:
            return await self._fail(interaction, cmd, "Couldn't ban - check my permissions and role hierarchy.", user, reason)
        except discord.HTTPException as e:
            return await self._fail(interaction, cmd, f"Ban failed: {e}", user, reason)
        db.add_infraction(interaction.guild.id, user.id, "ban", reason, interaction.user.id)
        embed = self._result("🔨 Member Banned", discord.Color.red(), user, interaction, reason, dm_sent=dm_sent)
        await self._done(interaction, cmd, embed, user, reason, "Banned")

    # ---------------- /unban ----------------

    @app_commands.command(name="unban", description="[Mod] Unban a user by ID")
    @app_commands.describe(user="User ID (or mention)", reason="Why they're being unbanned")
    async def unban(self, interaction: discord.Interaction, user: discord.User, reason: str = NO_REASON):
        cmd = "unban"
        if not can_use(interaction.user, cmd):
            return await self._fail(interaction, cmd, command_perms.NO_PERMS_MESSAGE, user, reason)
        try:
            await interaction.guild.unban(user, reason=self._audit_reason(interaction, reason))
        except discord.NotFound:
            return await self._fail(interaction, cmd, "That user isn't banned.", user, reason)
        except discord.Forbidden:
            return await self._fail(interaction, cmd, "Couldn't unban - check my permissions.", user, reason)
        except discord.HTTPException as e:
            return await self._fail(interaction, cmd, f"Unban failed: {e}", user, reason)
        embed = self._result("♻️ Member Unbanned", discord.Color.green(), user, interaction, reason)
        await self._done(interaction, cmd, embed, user, reason, "Unbanned")

    # ---------------- /kick ----------------

    @app_commands.command(name="kick", description="[Mod] Kick a member")
    @app_commands.describe(user="Mention or user ID", reason="Why they're being kicked")
    async def kick(self, interaction: discord.Interaction, user: discord.User, reason: str = NO_REASON):
        cmd = "kick"
        if not can_use(interaction.user, cmd):
            return await self._fail(interaction, cmd, command_perms.NO_PERMS_MESSAGE, user, reason)
        member = self._member(interaction, user)
        if member is None:
            return await self._fail(interaction, cmd, "That user isn't in this server.", user, reason)
        problem = self._target_problem(interaction, member)
        if problem:
            return await self._fail(interaction, cmd, problem, member, reason)

        dm_sent = await self._dm(member, interaction.guild, "kicked", reason)
        try:
            await member.kick(reason=self._audit_reason(interaction, reason))
        except discord.Forbidden:
            return await self._fail(interaction, cmd, "Couldn't kick - check my permissions and role hierarchy.", member, reason)
        except discord.HTTPException as e:
            return await self._fail(interaction, cmd, f"Kick failed: {e}", member, reason)
        db.add_infraction(interaction.guild.id, member.id, "kick", reason, interaction.user.id)
        embed = self._result("👢 Member Kicked", discord.Color.orange(), member, interaction, reason, dm_sent=dm_sent)
        await self._done(interaction, cmd, embed, member, reason, "Kicked")

    # ---------------- /timeout ----------------

    @app_commands.command(name="timeout", description="[Mod] Time a member out (10m, 2h, 1d) - or use 'off' to end it")
    @app_commands.describe(user="Mention or user ID",
                           duration="10m, 2h, 1d, 1h30m (a bare number is minutes, max 28d) - or 'off' to end their timeout",
                           reason="Why")
    async def timeout(self, interaction: discord.Interaction, user: discord.User, duration: str,
                      reason: str = NO_REASON):
        cmd = "timeout"
        if not can_use(interaction.user, cmd):
            return await self._fail(interaction, cmd, command_perms.NO_PERMS_MESSAGE, user, reason)
        if duration.strip().lower() in REMOVE_WORDS:
            return await self._remove_timeout(interaction, user, reason)
        delta = parse_timeout(duration)
        if delta is None:
            return await self._fail(interaction, cmd, "I couldn't read that duration - try `10m`, `2h`, `1d` or `1h30m`.", user, reason)
        if delta > MAX_TIMEOUT:
            return await self._fail(interaction, cmd, "Timeouts can be at most 28 days.", user, reason)
        member = self._member(interaction, user)
        if member is None:
            return await self._fail(interaction, cmd, "That user isn't in this server.", user, reason)
        problem = self._target_problem(interaction, member)
        if problem:
            return await self._fail(interaction, cmd, problem, member, reason)

        try:
            await member.timeout(delta, reason=self._audit_reason(interaction, reason))
        except discord.Forbidden:
            return await self._fail(interaction, cmd, "Couldn't time them out - check my permissions and role hierarchy.", member, reason)
        except discord.HTTPException as e:
            return await self._fail(interaction, cmd, f"Timeout failed: {e}", member, reason)
        pretty = human_duration(delta)
        dm_sent = await self._dm(member, interaction.guild, "timed out", reason, extra=pretty)
        db.add_infraction(interaction.guild.id, member.id, "timeout", f"{pretty} - {reason}", interaction.user.id)
        embed = self._result("⏱️ Member Timed Out", discord.Color.gold(), member, interaction, reason,
                             extra=[("Duration", pretty)], dm_sent=dm_sent)
        await self._done(interaction, cmd, embed, member, reason, f"Timed out for {pretty}")

    # ---------------- ending a timeout (/timeout <user> off) ----------------

    async def _remove_timeout(self, interaction: discord.Interaction, user: discord.User, reason: str):
        cmd = "timeout"
        member = self._member(interaction, user)
        if member is None:
            return await self._fail(interaction, cmd, "That user isn't in this server.", user, reason)
        if member.timed_out_until is None or member.timed_out_until <= discord.utils.utcnow():
            return await self._fail(interaction, cmd, "That member isn't timed out.", member, reason)
        problem = self._target_problem(interaction, member)
        if problem and problem != "You can't do that to yourself.":
            return await self._fail(interaction, cmd, problem, member, reason)
        try:
            await member.timeout(None, reason=self._audit_reason(interaction, reason))
        except discord.Forbidden:
            return await self._fail(interaction, cmd, "Couldn't remove the timeout - check my permissions.", member, reason)
        except discord.HTTPException as e:
            return await self._fail(interaction, cmd, f"Failed: {e}", member, reason)
        embed = self._result("✅ Timeout Removed", discord.Color.green(), member, interaction, reason)
        await self._done(interaction, cmd, embed, member, reason, "Timeout removed")

    # ---------------- /warn ----------------

    @app_commands.command(name="warn", description="[Mod] Warn a member (saved to their infractions)")
    @app_commands.describe(user="Mention or user ID", reason="What they're being warned for")
    async def warn(self, interaction: discord.Interaction, user: discord.User, reason: str):
        cmd = "warn"
        if not can_use(interaction.user, cmd):
            return await self._fail(interaction, cmd, command_perms.NO_PERMS_MESSAGE, user, reason)
        member = self._member(interaction, user)
        if member is None:
            return await self._fail(interaction, cmd, "That user isn't in this server.", user, reason)
        problem = self._target_problem(interaction, member)
        if problem:
            return await self._fail(interaction, cmd, problem, member, reason)

        db.add_infraction(interaction.guild.id, member.id, "warning", reason, interaction.user.id)
        total = db.count_infractions(interaction.guild.id, member.id, "warning")
        dm_sent = await self._dm(member, interaction.guild, "warned", reason)
        embed = self._result("⚠️ Member Warned", discord.Color.yellow(), member, interaction, reason,
                             extra=[("Total warnings", str(total))], dm_sent=dm_sent)
        await self._done(interaction, cmd, embed, member, reason, f"Warning #{total}")
        # Warnings don't fire any Discord event, so modlog.py can't see them - log them here.
        channel = gc.get_log_channel(interaction.guild, "mod-log-channel")
        if channel is not None:
            try:
                await channel.send(embed=embed)
            except discord.HTTPException:
                pass

    # ---------------- /mute and /unmute (mute role) ----------------

    def _mute_role(self, interaction):
        role_id = gc.get(interaction.guild.id, "mute-role")
        return interaction.guild.get_role(role_id) if role_id else None

    @app_commands.command(name="mute", description="[Mod] Mute a member (mute role) - run it again to unmute them")
    @app_commands.describe(user="Mention or user ID", reason="Why")
    async def mute(self, interaction: discord.Interaction, user: discord.User, reason: str = NO_REASON):
        cmd = "mute"
        if not can_use(interaction.user, cmd):
            return await self._fail(interaction, cmd, command_perms.NO_PERMS_MESSAGE, user, reason)
        role = self._mute_role(interaction)
        if role is None:
            return await self._fail(interaction, cmd, "The mute role isn't configured (or doesn't exist) - set it with `/set`.", user, reason)
        member = self._member(interaction, user)
        if member is None:
            return await self._fail(interaction, cmd, "That user isn't in this server.", user, reason)
        problem = self._target_problem(interaction, member)
        if problem:
            return await self._fail(interaction, cmd, problem, member, reason)
        if role in member.roles:  # already muted -> this run is the unmute
            return await self._unmute(interaction, user, reason)
        try:
            await member.add_roles(role, reason=self._audit_reason(interaction, reason))
        except discord.Forbidden:
            return await self._fail(interaction, cmd, "Couldn't add the mute role - check my permissions and role hierarchy.", member, reason)
        except discord.HTTPException as e:
            return await self._fail(interaction, cmd, f"Mute failed: {e}", member, reason)
        db.add_infraction(interaction.guild.id, member.id, "mute", reason, interaction.user.id)
        dm_sent = await self._dm(member, interaction.guild, "muted", reason)
        embed = self._result("🔇 Member Muted", discord.Color.dark_grey(), member, interaction, reason, dm_sent=dm_sent)
        await self._done(interaction, cmd, embed, member, reason, "Mute role added")

    async def _unmute(self, interaction: discord.Interaction, user: discord.User, reason: str):
        cmd = "mute"
        role = self._mute_role(interaction)
        if role is None:
            return await self._fail(interaction, cmd, "The mute role isn't configured (or doesn't exist) - set it with `/set`.", user, reason)
        member = self._member(interaction, user)
        if member is None:
            return await self._fail(interaction, cmd, "That user isn't in this server.", user, reason)
        if role not in member.roles:
            return await self._fail(interaction, cmd, "That member isn't muted.", member, reason)
        try:
            await member.remove_roles(role, reason=self._audit_reason(interaction, reason))
        except discord.Forbidden:
            return await self._fail(interaction, cmd, "Couldn't remove the mute role - check my permissions.", member, reason)
        except discord.HTTPException as e:
            return await self._fail(interaction, cmd, f"Unmute failed: {e}", member, reason)
        embed = self._result("🔊 Member Unmuted", discord.Color.green(), member, interaction, reason)
        await self._done(interaction, cmd, embed, member, reason, "Mute role removed")


async def setup(bot: commands.Bot):
    await bot.add_cog(Punishments(bot))
