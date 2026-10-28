"""
/debug - one-stop diagnostics for admins: bot info, which cogs loaded, every channel / role setting and
whether it resolves in this server, the bot's permissions, and the support / rate-limit settings.

The checks are built from guild_config's own setting lists and the files in cogs/, so they stay in sync
as settings and cogs are added or removed. The report goes out as separate messages (Discord caps one
message at 6000 characters of embeds), and any error while building it is reported instead of leaving
the command "thinking" forever.
"""
import datetime
import logging
import os
import pkgutil
import sys

import discord
from discord import app_commands
from discord.ext import commands

import command_perms
import config
import database as db
import guild_config as gc
import rcon
import support
from cogs import __path__ as COGS_PATH
from permissions import is_owner, owner_ids

BLURPLE = discord.Color.blurple()


def is_admin_perm(member: discord.Member) -> bool:
    if is_owner(member.id):
        return True
    if command_perms.granted():
        return True
    return member.guild_permissions.administrator


def _tag(guild_id: int, key: str) -> str:
    return " *(set by an owner)*" if gc.is_overridden(guild_id, key) else ""


def _check_channel(guild: discord.Guild, label: str, key: str, expected=None) -> str:
    cid = gc.get(guild.id, key)
    if not cid:
        return f"⚪ **{label}** - not set"
    channel = guild.get_channel(cid)
    tag = _tag(guild.id, key)
    if channel is None:
        return f"❌ **{label}** - ID `{cid}` not found in this server{tag}"
    if expected is not None and not isinstance(channel, expected):
        return f"⚠️ **{label}** - {channel.mention} is a {type(channel).__name__}, expected {expected.__name__}{tag}"
    me = guild.me
    if isinstance(channel, discord.TextChannel):
        perms = channel.permissions_for(me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            return f"⚠️ **{label}** - {channel.mention} but I can't post embeds there{tag}"
    return f"✅ **{label}** - {channel.mention}{tag}"


class Debug(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="debug", description="[Admin] Bot diagnostics - info, cogs, channels, roles and permissions")
    async def debug(self, interaction: discord.Interaction):
        if not is_admin_perm(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        private = not getattr(interaction, "is_prefix", False)
        await interaction.response.defer(ephemeral=private)
        try:
            embeds = self._build(interaction.guild)
        except Exception:
            logging.exception("/debug failed while building the report")
            return await interaction.followup.send(
                "❌ /debug hit an error while building the report - the details are in the bot's console log.",
                ephemeral=private)
        for embed in embeds:  # one message each: Discord limits a message to 6000 characters of embeds
            await interaction.followup.send(embed=embed, ephemeral=private)

    def _build(self, guild: discord.Guild) -> list[discord.Embed]:
        embeds = []

        # ---- Bot ----
        uptime = "unknown"
        if hasattr(self.bot, "start_time"):
            delta = datetime.datetime.now(datetime.timezone.utc) - self.bot.start_time
            hours, rem = divmod(int(delta.total_seconds()), 3600)
            uptime = f"{hours}h {rem // 60}m {rem % 60}s"
        try:
            db_size = f"{os.path.getsize(db.DB_PATH) / 1024:.1f} KB"
        except OSError:
            db_size = "not created yet"
        mode = support.get_mode(guild.id)
        limit = (f"{config.MEMBER_COMMAND_LIMIT} per {config.MEMBER_COMMAND_WINDOW}s"
                 if config.MEMBER_COMMAND_LIMIT > 0 else "off")
        bot_embed = discord.Embed(title="🔧 Debug - Bot", color=BLURPLE)
        bot_embed.add_field(name="Bot", value=f"{self.bot.user} (`{self.bot.user.id}`)", inline=False)
        bot_embed.add_field(name="Owners", value=" ".join(f"<@{u}>" for u in sorted(owner_ids())) or "None", inline=False)
        bot_embed.add_field(name="Uptime", value=uptime, inline=True)
        bot_embed.add_field(name="Latency", value=f"{self.bot.latency * 1000:.0f}ms", inline=True)
        bot_embed.add_field(name="Commands", value=str(len(self.bot.tree.get_commands())), inline=True)
        bot_embed.add_field(name="Support mode", value=support.LABELS[mode], inline=True)
        bot_embed.add_field(name="Open modmail", value=str(db.mm_count_open(guild.id)), inline=True)
        bot_embed.add_field(name="Member command limit", value=limit, inline=True)
        bot_embed.add_field(name="Minecraft RCON",
                            value=(f"✅ `{config.RCON_HOST}:{config.RCON_PORT}`" if rcon.configured()
                                   else "⚪ not configured (RCON_HOST / RCON_PASSWORD in .env)"), inline=False)
        bot_embed.add_field(name="Database", value=f"`{db.DB_PATH}` ({db_size})", inline=False)
        bot_embed.add_field(name="Versions", value=f"discord.py {discord.__version__} · Python {sys.version.split()[0]}", inline=False)
        embeds.append(bot_embed)

        # ---- Cogs: every module in cogs/ should be loaded ----
        expected = {f"cogs.{m.name}" for m in pkgutil.iter_modules(COGS_PATH)}
        loaded = set(self.bot.extensions)
        missing = sorted(expected - loaded)
        cogs_embed = discord.Embed(title="🔧 Debug - Cogs", color=discord.Color.red() if missing else discord.Color.green())
        cogs_embed.description = (f"✅ {len(expected - set(missing))}/{len(expected)} loaded" if not missing else
                                  f"⚠️ {len(missing)} not loaded - their commands won't work")
        if missing:
            cogs_embed.add_field(name="Not loaded", value="\n".join(f"❌ `{m}`" for m in missing)[:1024], inline=False)
            cogs_embed.set_footer(text="Check the bot's console for the import error.")
        embeds.append(cogs_embed)

        # ---- Channels ----
        lines = [_check_channel(guild, label, key) for key, (label, *_rest) in gc.CHANNEL_SETTINGS.items()]
        lines += [_check_channel(guild, label, key, discord.CategoryChannel)
                  for key, (label, _attr) in gc.CATEGORY_SETTINGS.items()]
        channels_embed = discord.Embed(title="🔧 Debug - Channels", description="\n".join(lines)[:4000], color=BLURPLE)
        channels_embed.set_footer(text="Change them with /setchannel (owners) or /set.")
        embeds.append(channels_embed)

        # ---- Roles ----
        role_lines = []
        for key, (label, _attr) in gc.ROLE_SETTINGS.items():
            rid = gc.get(guild.id, key)
            if not rid:
                role_lines.append(f"⚪ **{label}** - not set")
                continue
            role = guild.get_role(rid)
            if role is None:
                role_lines.append(f"❌ **{label}** - ID `{rid}` not found")
            elif role >= guild.me.top_role and key == "mute-role":
                role_lines.append(f"⚠️ **{label}** - {role.mention} is above my top role, so I can't give it")
            else:
                role_lines.append(f"✅ **{label}** - {role.mention}{_tag(guild.id, key)}")
        embeds.append(discord.Embed(title="🔧 Debug - Roles", description="\n".join(role_lines), color=BLURPLE))

        # ---- Bot permissions ----
        me = guild.me.guild_permissions
        wanted = ["administrator", "manage_roles", "manage_channels", "manage_messages", "ban_members",
                  "kick_members", "moderate_members", "view_audit_log"]
        perm_lines = [f"{'✅' if getattr(me, p, False) else '❌'} {p.replace('_', ' ').title()}" for p in wanted]
        cat_keys = [(k, l) for k, (l, _a) in gc.CATEGORY_SETTINGS.items()]
        perm_lines.append("")
        for k, l in cat_keys:
            category = guild.get_channel(gc.get(guild.id, k) or 0)
            short = l.split(" (")[0]
            if isinstance(category, discord.CategoryChannel):
                cp = category.permissions_for(guild.me)
                bad = [n for n, ok in (("manage_channels", cp.manage_channels), ("view_channel", cp.view_channel),
                                       ("send_messages", cp.send_messages)) if not ok]
                perm_lines.append(f"**{short}:** " + ("✅ ok" if not bad else f"❌ missing {', '.join(bad)}"))
            else:
                perm_lines.append(f"**{short}:** ⚪ not set")
        embeds.append(discord.Embed(title="🔧 Debug - Bot permissions", description="\n".join(perm_lines), color=BLURPLE))
        return embeds


async def setup(bot: commands.Bot):
    await bot.add_cog(Debug(bot))
