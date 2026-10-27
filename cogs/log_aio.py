"""
/log-aio - "all in one" logging setup.

Creates a private, staff-only place for every log the bot writes and points the bot at it, so nobody
has to make channels and paste IDs one by one:

  * Separate channels (default): a private "📋 Staff Logs" category with one channel per log type
    (general, moderation, staff actions, anti-raid, honeypot, applications, accepted/denied
    applications, ticket transcripts, modmail transcripts).
  * One channel: a single private "staff-logs" channel that receives every log.

Only the staff and admin roles (see /set) can see them, read-only; @everyone can't. The bot gets
view/send/embed/attach. The new channels are saved as per-server overrides (the same ones
/antiraid config uses), so they take effect immediately and win over .env values.

Running it again reuses the channels it made before (and re-applies their permissions) instead of
making duplicates, unless you switch layout.

Needs the bot to have Manage Channels (and Manage Roles to set the private permissions).
"""
import json
import logging

import discord
from discord import app_commands
from discord.ext import commands

import command_perms
import database as db
import guild_config as gc
from permissions import is_owner

STATE_KEY = "log_aio_state"
CATEGORY_NAME = "📋 Staff Logs"
SINGLE_NAME = "staff-logs"

# setting key -> (channel name, what it holds)
LOG_CHANNELS = {
    "log-channel": ("general-logs", "Unified log - anything without its own channel lands here."),
    "mod-log-channel": ("mod-logs", "Bans, unbans, kicks, timeouts, mutes and warnings - by anyone."),
    "staff-action-log-channel": ("staff-actions", "Moderation command usage, /say and purges."),
    "antiraid-log-channel": ("antiraid-logs", "Anti-raid spam and scam detections."),
    "honeypot-log-channel": ("honeypot-logs", "People who tripped the honeypot."),
    "application-log-channel": ("application-logs", "Submitted applications for review."),
    "application-accepted-channel": ("applications-accepted", "Accepted applications."),
    "application-denied-channel": ("applications-denied", "Denied applications."),
    "transcript-channel": ("ticket-transcripts", "Transcripts of closed tickets."),
    "modmail-log-channel": ("modmail-logs", "Transcripts of closed modmail conversations."),
}


def is_admin_perm(member: discord.Member) -> bool:
    if is_owner(member.id) or command_perms.granted():
        return True
    return member.guild_permissions.administrator


def load_state(guild_id: int) -> dict:
    try:
        return json.loads(db.get_setting(guild_id, STATE_KEY, "{}")) or {}
    except (ValueError, TypeError):
        return {}


class LogAIO(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @staticmethod
    def _overwrites(guild: discord.Guild, staff_roles: list[discord.Role]):
        ow = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, embed_links=True, attach_files=True,
                read_message_history=True,
            ),
        }
        for role in staff_roles:
            ow[role] = discord.PermissionOverwrite(view_channel=True, read_message_history=True, send_messages=False)
        return ow

    @app_commands.command(name="log-aio", description="[Admin] Create private staff-only log channels and use them for every log")
    @app_commands.describe(layout="One channel per log type (default), or a single channel for everything")
    @app_commands.choices(layout=[
        app_commands.Choice(name="Separate channels (one per log type)", value="separate"),
        app_commands.Choice(name="One channel for everything", value="single"),
    ])
    async def log_aio(self, interaction: discord.Interaction, layout: app_commands.Choice[str] = None):
        if not is_admin_perm(interaction.user):
            return await interaction.response.send_message(command_perms.NO_PERMS_MESSAGE, ephemeral=True)
        guild = interaction.guild
        private = not getattr(interaction, "is_prefix", False)
        mode = layout.value if layout else "separate"

        async def fail(text: str):
            embed = discord.Embed(title="❌ Couldn't do that", description=text, color=discord.Color.red())
            await interaction.response.send_message(embed=embed, ephemeral=private)

        if not guild.me.guild_permissions.manage_channels:
            return await fail("I need the **Manage Channels** permission (and **Manage Roles**, to make them private).")
        staff_roles = [r for r in (guild.get_role(gc.get(guild.id, "staff-role")),
                                   guild.get_role(gc.get(guild.id, "admin-role"))) if r is not None]
        if not staff_roles:
            return await fail("No staff or admin role is set up, so nobody could see the channels. "
                              "Set them first with `/set` (Staff Role / Admin Role).")

        await interaction.response.defer(ephemeral=private)
        overwrites = self._overwrites(guild, staff_roles)
        state = load_state(guild.id)
        reuse = state if state.get("layout") == mode else {}
        saved = dict(reuse.get("channels", {}))
        created, reused = [], []
        replaced_old = bool(state) and state.get("layout") != mode

        async def get_or_create(key: str, name: str, topic: str, category):
            ch = guild.get_channel(saved.get(key, 0)) if saved.get(key) else None
            if isinstance(ch, discord.TextChannel):
                await ch.edit(overwrites=overwrites, reason=f"/log-aio by {interaction.user}")
                reused.append(ch)
                return ch
            ch = await guild.create_text_channel(
                name, category=category, overwrites=overwrites, topic=topic[:1024],
                reason=f"/log-aio by {interaction.user}")
            created.append(ch)
            return ch

        try:
            if mode == "single":
                ch = await get_or_create("all", SINGLE_NAME, "Every log the bot writes (staff only).", None)
                mapping = {key: ch for key in LOG_CHANNELS}
                new_state = {"layout": mode, "channels": {"all": ch.id}}
            else:
                category = guild.get_channel(reuse.get("category", 0)) if reuse.get("category") else None
                if not isinstance(category, discord.CategoryChannel):
                    category = await guild.create_category(CATEGORY_NAME, overwrites=overwrites,
                                                           reason=f"/log-aio by {interaction.user}")
                else:
                    await category.edit(overwrites=overwrites, reason=f"/log-aio by {interaction.user}")
                mapping = {}
                for key, (name, topic) in LOG_CHANNELS.items():
                    mapping[key] = await get_or_create(key, name, topic, category)
                new_state = {"layout": mode, "category": category.id,
                             "channels": {k: c.id for k, c in mapping.items()}}
        except discord.Forbidden:
            return await interaction.followup.send(
                embed=discord.Embed(title="❌ Couldn't do that", color=discord.Color.red(),
                                    description="Discord refused - I need **Manage Channels** and **Manage Roles**, "
                                                "and my role must be above the staff roles."),
                ephemeral=private)
        except discord.HTTPException as e:
            logging.exception("/log-aio failed")
            return await interaction.followup.send(
                embed=discord.Embed(title="❌ Couldn't do that", description=f"Discord returned an error: {e}",
                                    color=discord.Color.red()), ephemeral=private)

        # Point every log at its new channel, and remember what we made so a re-run reuses it.
        for key, ch in mapping.items():
            gc.set_override(guild.id, key, ch.id)
        db.set_setting(guild.id, STATE_KEY, json.dumps(new_state))
        logging.info("/log-aio by %s (%s): layout=%s created=%d reused=%d", interaction.user, interaction.user.id,
                     mode, len(created), len(reused))

        # A first message in each new channel confirms the bot can post there.
        for ch in created:
            try:
                if mode == "single":
                    desc = "Every log the bot writes lands here. Only staff can see this channel."
                else:
                    desc = next((t for _k, (n, t) in LOG_CHANNELS.items() if n == ch.name), "Bot logs.")
                await ch.send(embed=discord.Embed(title="📋 Logging set up", description=desc,
                                                  color=discord.Color.blurple()))
            except discord.HTTPException:
                pass

        if mode == "single":
            body = f"Everything now logs to {mapping['log-channel'].mention}."
        else:
            body = "\n".join(f"**{LOG_CHANNELS[k][0]}** - {c.mention}" for k, c in mapping.items())
        embed = discord.Embed(title="✅ Logging set up", description=body[:4000], color=discord.Color.green())
        embed.add_field(name="Who can see it", inline=False,
                        value=", ".join(r.mention for r in staff_roles) + " (read-only). @everyone can't.")
        embed.add_field(name="Result", inline=False,
                        value=f"{len(created)} created, {len(reused)} reused. All log settings now point here.")
        if replaced_old:
            embed.set_footer(text="Channels from your previous layout were left in place - delete them if you don't need them.")
        await interaction.followup.send(embed=embed, ephemeral=private)


async def setup(bot: commands.Bot):
    await bot.add_cog(LogAIO(bot))
