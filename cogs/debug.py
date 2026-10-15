import os
import time
import discord
from discord import app_commands
from discord.ext import commands

from permissions import is_owner
import command_perms
import database as db
import guild_config as gc
from permissions import owner_ids, is_owner


def is_admin_perm(member: discord.Member) -> bool:
    if is_owner(member.id):
        return True
    if command_perms.granted():
        return True
    return member.guild_permissions.administrator


EXPECTED_COGS = [
    "cogs.tickets", "cogs.moderation_tools", "cogs.applications",
    "cogs.utility", "cogs.honeypot", "cogs.antiraid",
    "cogs.debug", "cogs.modlog", "cogs.owner", "cogs.prefixcmds", "cogs.settings_cmd",
]

# name, guild_config setting key, expected discord.py type (or None for "any channel")
CHANNEL_CHECKS = [
    ("Ticket Category", "ticket-category", discord.CategoryChannel),
    ("Application Log Channel", "application-log-channel", None),
    ("Applications Accepted Channel", "application-accepted-channel", None),
    ("Applications Denied Channel", "application-denied-channel", None),
    ("Unified Log Channel", "log-channel", None),
    ("Moderation Log Channel", "mod-log-channel", None),
    ("Transcript Channel", "transcript-channel", None),
    ("Staff Action Log", "staff-action-log-channel", None),
    ("Honeypot Log (default)", "honeypot-log-channel", None),
    ("Anti-Raid Log Channel", "antiraid-log-channel", None),
    ("Ticket Panel Channel", "ticket-panel", None),
    ("Staff Promotions Channel", "staff-promotions-channel", None),
    ("Staff Infractions Channel", "staff-infractions-channel", None),
    ("Support Channel", "support-channel", None),
]

ROLE_CHECKS = [
    ("Staff Role", "staff-role"),
    ("Admin Role", "admin-role"),
    ("Base Member Role (protected)", "base-member-role"),
    ("Mute Role", "mute-role"),
]


class Debug(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="debug", description="[Admin] Show bot diagnostics - cogs, channels, roles, and permissions")
    async def debug(self, interaction: discord.Interaction):
        if not is_admin_perm(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild

        # ---- Cogs ----
        loaded = set(self.bot.extensions.keys())
        cog_lines = []
        for ext in EXPECTED_COGS:
            cog_lines.append(f"{'✅' if ext in loaded else '❌'} `{ext}`")
        unexpected = loaded - set(EXPECTED_COGS)
        for ext in unexpected:
            cog_lines.append(f"❓ `{ext}` (loaded but not in expected list)")

        cogs_embed = discord.Embed(title="🔧 Debug — Cogs", color=discord.Color.blurple())
        cogs_embed.description = "\n".join(cog_lines)
        cogs_embed.add_field(name="Latency", value=f"{self.bot.latency * 1000:.0f}ms", inline=True)
        cogs_embed.add_field(name="Commands", value=str(len(self.bot.tree.get_commands(guild=guild) or self.bot.tree.get_commands())), inline=True)

        # ---- Channels (from .env) ----
        channel_lines = []
        for name, key, expected_type in CHANNEL_CHECKS:
            cid = gc.get(guild.id, key)
            tag = " *(custom)*" if gc.is_overridden(guild.id, key) else " *(default)*"
            if not cid:
                channel_lines.append(f"⚪ **{name}** — not configured (0)")
                continue
            channel = guild.get_channel(cid)
            if channel is None:
                channel_lines.append(f"❌ **{name}** — ID `{cid}` not found in this server{tag}")
            elif expected_type is not None and not isinstance(channel, expected_type):
                channel_lines.append(
                    f"⚠️ **{name}** — found #{channel.name}, but it's a {type(channel).__name__}, "
                    f"expected {expected_type.__name__}{tag}"
                )
            else:
                channel_lines.append(f"✅ **{name}** — {channel.mention}{tag}")

        channels_embed = discord.Embed(title="🔧 Debug — Channels", color=discord.Color.blurple())
        channels_embed.description = "\n".join(channel_lines)[:4000]
        channels_embed.set_footer(text="Set these IDs in .env.")

        # ---- Roles ----
        role_lines = []
        for name, key in ROLE_CHECKS:
            rid = gc.get(guild.id, key)
            tag = " *(custom)*" if gc.is_overridden(guild.id, key) else " *(default)*"
            if not rid:
                role_lines.append(f"⚪ **{name}** — not configured (0)")
                continue
            role = guild.get_role(rid)
            role_lines.append(f"✅ **{name}** — {role.mention}{tag}" if role else f"❌ **{name}** — ID `{rid}` not found{tag}")

        roles_embed = discord.Embed(title="🔧 Debug — Roles", color=discord.Color.blurple())
        roles_embed.description = "\n".join(role_lines)
        roles_embed.add_field(
            name="Owner(s)",
            value=" ".join(f"<@{uid}>" for uid in sorted(owner_ids())),
            inline=False,
        )

        # ---- Bot permissions in key channels ----
        perm_lines = []
        ticket_category_id = gc.get(guild.id, "ticket-category")
        category = guild.get_channel(ticket_category_id) if ticket_category_id else None
        if category:
            perms = category.permissions_for(guild.me)
            needed = {
                "manage_channels": perms.manage_channels,
                "send_messages": perms.send_messages,
                "view_channel": perms.view_channel,
                "manage_permissions": perms.manage_permissions,
            }
            missing = [k for k, v in needed.items() if not v]
            perm_lines.append(
                f"**Ticket Category:** " + ("✅ all needed permissions present" if not missing
                else f"❌ missing: {', '.join(missing)}")
            )
        else:
            perm_lines.append("**Ticket Category:** ⚪ not resolvable (see Channels tab)")

        bot_perms = guild.me.guild_permissions
        key_perms = ["administrator", "manage_roles", "manage_channels", "manage_messages",
                     "ban_members", "kick_members", "moderate_members", "manage_threads"]
        perm_lines.append("\n**Server-wide permissions:**")
        for p in key_perms:
            perm_lines.append(f"{'✅' if getattr(bot_perms, p, False) else '❌'} {p.replace('_', ' ').title()}")

        perms_embed = discord.Embed(title="🔧 Debug — Bot Permissions", color=discord.Color.blurple())
        perms_embed.description = "\n".join(perm_lines)

        # ---- System ----
        db_size = "unknown"
        try:
            db_size = f"{os.path.getsize(db.DB_PATH) / 1024:.1f} KB"
        except OSError:
            db_size = "file not found yet"


        sys_embed = discord.Embed(title="🔧 Debug — System", color=discord.Color.blurple())
        sys_embed.add_field(name="Database Path", value=f"`{db.DB_PATH}`", inline=False)
        sys_embed.add_field(name="Database Size", value=db_size, inline=True)
        sys_embed.add_field(name="discord.py", value=discord.__version__, inline=True)
        sys_embed.add_field(name="This Guild ID", value=str(guild.id), inline=True)

        await interaction.followup.send(
            embeds=[cogs_embed, channels_embed, roles_embed, perms_embed, sys_embed], ephemeral=True,
        )

    @app_commands.command(name="debug-ping", description="[Admin] Quick latency + uptime check")
    async def debug_ping(self, interaction: discord.Interaction):
        if not is_admin_perm(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        start = time.perf_counter()
        await interaction.response.send_message("Checking...", ephemeral=True)
        rest_ms = (time.perf_counter() - start) * 1000
        await interaction.edit_original_response(
            content=f"Gateway latency: **{self.bot.latency * 1000:.0f}ms**\nREST round-trip: **{rest_ms:.0f}ms**"
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Debug(bot))
