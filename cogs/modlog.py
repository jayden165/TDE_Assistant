"""Moderation log - records moderation actions taken by ANYONE in the server (not just through
this bot's commands): bans, unbans, kicks, timeouts (and removals), and mute-role mutes.

It listens to Discord's own events and reads the audit log to work out who did it and why, so a
ban done from Discord's member menu, by another bot, or through /ban, /kick and friends all show up the
same way. Logs go to the Moderation Log Channel (MOD_LOG_CHANNEL_ID in .env), falling back to the
unified log channel if that isn't set.

Needs the bot to have "View Audit Log" to show the moderator and reason; without it the action
is still logged, just with "Unknown" as the moderator.
"""
import asyncio
import logging

import discord
from discord.ext import commands

import guild_config as gc

AUDIT_MAX_AGE_SECONDS = 20  # an audit entry older than this isn't the action we just saw


class ModLog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---------------- helpers ----------------

    async def _send(self, guild: discord.Guild, embed: discord.Embed):
        channel = gc.get_log_channel(guild, "mod-log-channel")
        if channel is None:
            return
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            pass

    async def _audit_entry(self, guild: discord.Guild, action: discord.AuditLogAction, target_id: int,
                           attempts: int = 3, first_delay: float = 1.0):
        """Most recent matching audit log entry for this target, or None. The audit log can lag
        a moment behind the gateway event, so this retries a few times before giving up."""
        if not guild.me.guild_permissions.view_audit_log:
            return None
        for attempt in range(attempts):
            await asyncio.sleep(first_delay if attempt == 0 else 1.5)
            try:
                async for entry in guild.audit_logs(limit=10, action=action):
                    if entry.target is None or entry.target.id != target_id:
                        continue
                    age = (discord.utils.utcnow() - entry.created_at).total_seconds()
                    if age <= AUDIT_MAX_AGE_SECONDS:
                        return entry
            except (discord.Forbidden, discord.HTTPException):
                return None
        return None

    @staticmethod
    def _build(title: str, color: discord.Color, user: discord.abc.User, entry=None, extra=None) -> discord.Embed:
        embed = discord.Embed(
            title=title,
            description=f"{user.mention} (`{user}`)",
            color=color,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name="Moderator", value=entry.user.mention if entry and entry.user else "Unknown", inline=True)
        for name, value in (extra or []):
            embed.add_field(name=name, value=value, inline=True)
        embed.add_field(name="Reason", value=(entry.reason if entry and entry.reason else "No reason given")[:1024],
                        inline=False)
        embed.set_footer(text=f"User ID: {user.id}")
        return embed

    async def _safely(self, coro):
        """Runs a logging task in the background without ever letting its errors escape."""
        try:
            await coro
        except Exception:
            logging.exception("modlog task failed")

    # ---------------- bans / unbans ----------------

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.abc.User):
        self.bot.loop.create_task(self._safely(self._log_ban(guild, user)))

    async def _log_ban(self, guild, user):
        entry = await self._audit_entry(guild, discord.AuditLogAction.ban, user.id)
        await self._send(guild, self._build("🔨 Member Banned", discord.Color.red(), user, entry))

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        self.bot.loop.create_task(self._safely(self._log_unban(guild, user)))

    async def _log_unban(self, guild, user):
        entry = await self._audit_entry(guild, discord.AuditLogAction.unban, user.id)
        await self._send(guild, self._build("♻️ Member Unbanned", discord.Color.green(), user, entry))

    # ---------------- kicks ----------------

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        # Every leave fires this, kicks included - so check the audit log for a matching kick.
        # Single attempt (not the usual retries) because most leaves are NOT kicks.
        self.bot.loop.create_task(self._safely(self._log_kick_if_kicked(member)))

    async def _log_kick_if_kicked(self, member: discord.Member):
        entry = await self._audit_entry(member.guild, discord.AuditLogAction.kick, member.id,
                                        attempts=1, first_delay=2.0)
        if entry is None:
            return
        await self._send(member.guild, self._build("👢 Member Kicked", discord.Color.orange(), member, entry))

    # ---------------- timeouts and mutes ----------------

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        now = discord.utils.utcnow()

        # Timeout applied / changed / removed
        if before.timed_out_until != after.timed_out_until:
            if after.timed_out_until and after.timed_out_until > now:
                self.bot.loop.create_task(self._safely(self._log_timeout(after, after.timed_out_until)))
            elif before.timed_out_until and before.timed_out_until > now and after.timed_out_until is None:
                self.bot.loop.create_task(self._safely(self._log_timeout(after, None)))

        # Mute role gained / lost
        mute_role_id = gc.get(after.guild.id, "mute-role")
        if mute_role_id:
            had = any(r.id == mute_role_id for r in before.roles)
            has = any(r.id == mute_role_id for r in after.roles)
            if had != has:
                self.bot.loop.create_task(self._safely(self._log_mute_role(after, muted=has)))

    async def _log_timeout(self, member: discord.Member, until):
        entry = await self._audit_entry(member.guild, discord.AuditLogAction.member_update, member.id)
        if until is not None:
            embed = self._build("⏱️ Member Timed Out", discord.Color.gold(), member, entry,
                                extra=[("Until", discord.utils.format_dt(until, "R"))])
        else:
            embed = self._build("✅ Timeout Removed", discord.Color.green(), member, entry)
        await self._send(member.guild, embed)

    async def _log_mute_role(self, member: discord.Member, muted: bool):
        entry = await self._audit_entry(member.guild, discord.AuditLogAction.member_role_update, member.id)
        if muted:
            embed = self._build("🔇 Member Muted", discord.Color.dark_grey(), member, entry)
        else:
            embed = self._build("🔊 Member Unmuted", discord.Color.green(), member, entry)
        await self._send(member.guild, embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(ModLog(bot))
