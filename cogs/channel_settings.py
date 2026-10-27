"""
/setchannel - lets any bot owner choose which channel each setting uses (ticket panel, ticket
transcripts, every log channel, application channels, the ticket category), without needing the
.env file or the /set setup account.

    /setchannel                                   lists every setting and what it's set to
    /setchannel setting:Ticket Panel Channel channel:#tickets
    /setchannel setting:Ticket Panel Channel clear:True     goes back to the .env value (if any)
    s! setchannel ticket-panel #tickets                     same thing as a text command

Choices are saved as per-server overrides (the same mechanism /antiraid config and /log-aio use), so
they apply immediately, survive restarts and win over .env. The bot warns if it can't post in the
channel you picked. Every change is written to the console and the staff action log.
"""
import logging

import discord
from discord import app_commands
from discord.ext import commands

import command_perms
import config
import guild_config as gc
from permissions import is_owner

# setting key -> (label, kind)   kind: channel | category
SETTINGS: dict[str, tuple[str, str]] = {}
for _key, (_label, *_rest) in gc.CHANNEL_SETTINGS.items():
    SETTINGS[_key] = (_label, "channel")
for _key, (_label, _attr) in gc.CATEGORY_SETTINGS.items():
    SETTINGS[_key] = (_label, "category")

CHOICES = [app_commands.Choice(name=label[:100], value=key) for key, (label, _k) in SETTINGS.items()]


class ChannelSettings(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @staticmethod
    def _describe(guild: discord.Guild, key: str) -> str:
        value = gc.get(guild.id, key)
        if not value:
            return "⚪ not set"
        channel = guild.get_channel(value)
        source = "set by an owner" if gc.is_overridden(guild.id, key) else "from .env"
        if channel is None:
            return f"❌ `{value}` (not found in this server) - {source}"
        return f"✅ {channel.mention} - {source}"

    @app_commands.command(name="setchannel", description="[Owner] Choose which channel each setting uses (ticket panel, logs, applications...)")
    @app_commands.describe(
        setting="Which setting to change (leave empty to see them all)",
        channel="The channel (or category, for the ticket category)",
        clear="Remove your choice so the setting goes back to its .env value",
    )
    @app_commands.choices(setting=CHOICES)
    async def setchannel(self, interaction: discord.Interaction, setting: app_commands.Choice[str] = None,
                         channel: discord.abc.GuildChannel = None, clear: bool = False):
        private = not getattr(interaction, "is_prefix", False)
        guild = interaction.guild

        async def reply(embed: discord.Embed):
            await interaction.response.send_message(embed=embed, ephemeral=private)

        def error(text: str) -> discord.Embed:
            return discord.Embed(title="❌ Couldn't do that", description=text, color=discord.Color.red())

        if not is_owner(interaction.user.id) or guild is None:
            return await reply(error(command_perms.NO_PERMS_MESSAGE))

        if setting is None:
            lines = [f"**{label}** - {self._describe(guild, key)}" for key, (label, _k) in SETTINGS.items()]
            embed = discord.Embed(title="📍 Channel settings", description="\n".join(lines)[:4000],
                                  color=discord.Color.blurple())
            embed.set_footer(text="Change one: /setchannel setting:<name> channel:#channel")
            return await reply(embed)

        key = setting.value
        label, kind = SETTINGS[key]

        if clear:
            had = gc.is_overridden(guild.id, key)
            gc.clear_override(guild.id, key)
            logging.info("setchannel: %s cleared %s", interaction.user.id, key)
            await self._audit(interaction, label, "cleared (back to .env)")
            text = (f"♻️ **{label}** is back to its `.env` value: {self._describe(guild, key)}" if had
                    else f"**{label}** had no owner choice. It's {self._describe(guild, key)}")
            return await reply(discord.Embed(description=text, color=discord.Color.green()))

        if channel is None:
            return await reply(discord.Embed(
                title=label, color=discord.Color.blurple(),
                description=f"Currently: {self._describe(guild, key)}\nGive a `channel` to change it."))

        warning = ""
        if kind == "category":
            if not isinstance(channel, discord.CategoryChannel):
                return await reply(error(f"**{label}** needs a category, and {channel.mention} is a channel."))
        else:
            if not isinstance(channel, discord.TextChannel):
                return await reply(error(f"**{label}** needs a text channel - {channel.mention} isn't one."))
            perms = channel.permissions_for(guild.me)
            missing = [name for name, ok in (("View Channel", perms.view_channel),
                                             ("Send Messages", perms.send_messages),
                                             ("Embed Links", perms.embed_links)) if not ok]
            if missing:
                warning = f"\n⚠️ Saved, but I'm missing **{', '.join(missing)}** there, so it won't work until you fix that."

        gc.set_override(guild.id, key, channel.id)
        logging.info("setchannel: %s set %s -> %s", interaction.user.id, key, channel.id)
        await self._audit(interaction, label, channel.mention)
        await reply(discord.Embed(
            description=f"✅ **{label}** is now {channel.mention}.{warning}",
            color=discord.Color.orange() if warning else discord.Color.green()))

    async def _audit(self, interaction: discord.Interaction, label: str, value: str):
        """Note the change in the staff action log. Never raises."""
        try:
            log = gc.get_log_channel(interaction.guild, "staff-action-log-channel")
            if log is None:
                return
            embed = discord.Embed(title="📍 Channel setting changed", color=discord.Color.orange(),
                                  timestamp=discord.utils.utcnow())
            embed.add_field(name="Changed by", value=f"{interaction.user.mention} ({interaction.user.id})", inline=True)
            embed.add_field(name="Setting", value=label, inline=True)
            embed.add_field(name="Now", value=value, inline=True)
            await log.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            logging.exception("couldn't write the channel setting audit log")


async def setup(bot: commands.Bot):
    await bot.add_cog(ChannelSettings(bot))
