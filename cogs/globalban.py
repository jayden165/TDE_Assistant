"""
Global ban: a ban in any server the bot is in is mirrored to every other server it's in.

  * Someone is banned in server A (by a person, by this bot's /ban, the honeypot - anything that fires
    Discord's ban event) -> the bot bans them in every other server it's in, with the reason
    "[Global ban] Banned in <A>: <original reason>". Servers where they're already banned are left alone.
  * Someone is unbanned in server A -> the bot lifts the global bans it made elsewhere. A ban another
    server placed on its own (no "[Global ban]" tag) is NOT touched, unless GLOBAL_UNBAN_ALL=true in .env.
  * Loops are impossible: the bans this cog makes are remembered for a minute so their own events are
    ignored instead of spreading again.
  * Never globally banned: this bot, and bot owners (so one mistaken ban can't lock out the people who run it).
  * A summary - which servers got the ban and which couldn't (usually missing Ban Members) - is posted to
    the origin server's mod log, and a notice to each affected server's mod log.

Needs: the bot in each server with the Ban Members permission, and Discord's moderation events (on by
default). Turn the whole thing off with GLOBAL_BAN_ENABLED=false in .env.
"""
import logging
import time

import discord
from discord.ext import commands

import config
import guild_config as gc
from permissions import is_owner

MARK = "[Global ban]"
EXPECT_SECONDS = 90


class GlobalBan(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._expected: dict[tuple[int, int, str], float] = {}  # (guild, user, "ban"/"unban") we caused ourselves

    # ---------------- loop protection ----------------

    def _expect(self, key: tuple[int, int, str]):
        self._expected[key] = time.monotonic()

    def _consume(self, key: tuple[int, int, str]) -> bool:
        """True if this event was caused by us (and forgets it)."""
        now = time.monotonic()
        for k in [k for k, t in self._expected.items() if now - t > EXPECT_SECONDS]:
            del self._expected[k]
        return self._expected.pop(key, None) is not None

    # ---------------- reporting ----------------

    @staticmethod
    async def _post(guild: discord.Guild, embed: discord.Embed):
        channel = gc.get_log_channel(guild, "mod-log-channel")
        if channel is None:
            return
        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass

    async def _report(self, origin: discord.Guild, user: discord.User, title: str, color: discord.Color,
                      done: list, skipped: list, failed: list, note: str):
        embed = discord.Embed(title=title, color=color, timestamp=discord.utils.utcnow(),
                              description=f"{user.mention} (`{user}` · {user.id})\n{note}")
        embed.add_field(name="Applied", value=f"{len(done)} server(s)", inline=True)
        embed.add_field(name="Skipped", value=f"{len(skipped)} server(s)", inline=True)
        embed.add_field(name="Failed", value=f"{len(failed)} server(s)", inline=True)
        if failed:
            embed.add_field(name="Couldn't update", inline=False,
                            value="\n".join(f"**{g.name}** - {why}" for g, why in failed)[:1024])
        await self._post(origin, embed)

    # ---------------- ban ----------------

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        if not config.GLOBAL_BAN_ENABLED or self._consume((guild.id, user.id, "ban")):
            return
        if user.id == self.bot.user.id or is_owner(user.id):
            return await self._post(guild, discord.Embed(
                title="🌐 Global ban skipped", color=discord.Color.orange(),
                description=f"{user.mention} is the bot or one of its owners, so the ban stays in this server only."))
        try:
            original = (await guild.fetch_ban(user)).reason or "No reason given"
        except discord.HTTPException:
            original = "No reason given"
        reason = f"{MARK} Banned in {guild.name}: {original}"[:512]

        done, skipped, failed = [], [], []
        for target in self.bot.guilds:
            if target.id == guild.id:
                continue
            if not target.me.guild_permissions.ban_members:
                failed.append((target, "I don't have Ban Members there"))
                continue
            try:
                await target.fetch_ban(user)
                skipped.append(target)  # already banned there - leave their reason alone
                continue
            except discord.NotFound:
                pass
            except discord.HTTPException as e:
                failed.append((target, f"couldn't check ({type(e).__name__})"))
                continue
            key = (target.id, user.id, "ban")
            self._expect(key)
            try:
                await target.ban(user, reason=reason, delete_message_seconds=0)
                done.append(target)
            except discord.Forbidden:
                self._expected.pop(key, None)
                failed.append((target, "my role is below theirs, or I lack permission"))
            except discord.HTTPException as e:
                self._expected.pop(key, None)
                failed.append((target, f"Discord error ({type(e).__name__})"))
        logging.info("global ban of %s from %s: applied=%d skipped=%d failed=%d", user.id, guild.name,
                     len(done), len(skipped), len(failed))
        if not (done or skipped or failed):
            return  # the bot is only in this one server
        await self._report(guild, user, "🌐 Global ban", discord.Color.red(), done, skipped, failed,
                           f"Banned in **{guild.name}** - mirrored to the bot's other servers.\n**Reason:** {original}")
        for target in done:
            await self._post(target, discord.Embed(
                title="🌐 Global ban applied", color=discord.Color.red(),
                description=f"{user.mention} (`{user}` · {user.id}) was banned in **{guild.name}**, so they were banned here too.\n"
                            f"**Reason:** {original}"))

    # ---------------- unban ----------------

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        if not config.GLOBAL_BAN_ENABLED or self._consume((guild.id, user.id, "unban")):
            return
        done, skipped, failed = [], [], []
        for target in self.bot.guilds:
            if target.id == guild.id:
                continue
            if not target.me.guild_permissions.ban_members:
                failed.append((target, "I don't have Ban Members there"))
                continue
            try:
                entry = await target.fetch_ban(user)
            except discord.NotFound:
                continue  # not banned there - nothing to do
            except discord.HTTPException as e:
                failed.append((target, f"couldn't check ({type(e).__name__})"))
                continue
            if not (entry.reason or "").startswith(MARK) and not config.GLOBAL_UNBAN_ALL:
                skipped.append(target)  # that server banned them on its own - its call
                continue
            key = (target.id, user.id, "unban")
            self._expect(key)
            try:
                await target.unban(user, reason=f"{MARK} Unbanned in {guild.name}")
                done.append(target)
            except discord.HTTPException as e:
                self._expected.pop(key, None)
                failed.append((target, f"Discord error ({type(e).__name__})"))
        logging.info("global unban of %s from %s: lifted=%d skipped=%d failed=%d", user.id, guild.name,
                     len(done), len(skipped), len(failed))
        if not (done or skipped or failed):
            return
        note = f"Unbanned in **{guild.name}** - lifted the global ban elsewhere."
        if skipped:
            note += f"\n{len(skipped)} server(s) banned them on their own, so those bans stay."
        await self._report(guild, user, "🌐 Global unban", discord.Color.green(), done, skipped, failed, note)
        for target in done:
            await self._post(target, discord.Embed(
                title="🌐 Global ban lifted", color=discord.Color.green(),
                description=f"{user.mention} (`{user}` · {user.id}) was unbanned in **{guild.name}**, so they were unbanned here too."))


async def setup(bot: commands.Bot):
    await bot.add_cog(GlobalBan(bot))
