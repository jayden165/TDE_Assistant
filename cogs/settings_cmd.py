"""
/set - lets the setup user (permissions.SETUP_USER_ID) change the bot's IDs from Discord instead of
editing .env by hand: the server ID, staff/admin/mute roles, every log/ticket/application channel,
the ticket category and the rules link.

Each change is written to the .env file (so it survives a restart) and applied to the running bot
straight away. Any older per-server override for the same setting is cleared so the new value wins.
Changing the server ID re-syncs the slash commands to the new server.
"""
import logging
import os
import re

import discord
from discord import app_commands
from discord.ext import commands

import config
import database as db
import guild_config as gc
import permissions

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(config.__file__)), ".env")

# setting key -> (label, env var name, kind)   kind: server | channel | role | category | text
SETTINGS: dict[str, tuple[str, str, str]] = {"server-id": ("Server ID", "GUILD_ID", "server")}
for _key, (_label, _attr, *_rest) in gc.CHANNEL_SETTINGS.items():
    SETTINGS[_key] = (_label, _attr, "channel")
for _key, (_label, _attr) in gc.ROLE_SETTINGS.items():
    SETTINGS[_key] = (_label, _attr, "role")
for _key, (_label, _attr) in gc.CATEGORY_SETTINGS.items():
    SETTINGS[_key] = (_label, _attr, "category")
SETTINGS["rules-url"] = ("Rules Link (button under /rules)", "RULES_URL", "text")

CHOICES = [app_commands.Choice(name=label[:100], value=key) for key, (label, _e, _k) in SETTINGS.items()]
CLEAR_WORDS = {"clear", "none", "reset", "remove", "0"}


def write_env(name: str, value: str):
    """Sets NAME=value in the .env file (replacing the line if it exists), atomically."""
    lines = []
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, encoding="utf-8") as f:
            lines = f.read().splitlines()
    pattern = re.compile(r"^\s*" + re.escape(name) + r"\s*=")
    new_line = f"{name}={value}"
    for i, line in enumerate(lines):
        if pattern.match(line):
            lines[i] = new_line
            break
    else:
        lines.append(new_line)
    tmp = ENV_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, ENV_PATH)


def extract_id(raw: str):
    """An ID from plain digits or a <#channel> / <@&role> mention."""
    m = re.search(r"\d{15,22}", raw or "")
    return int(m.group()) if m else None


def clear_overrides(key: str):
    """Drops any saved per-server override of this setting (e.g. from /antiraid config)."""
    conn = db.get_conn()
    conn.execute("DELETE FROM settings WHERE key=?", (f"cfg:{key}",))
    conn.commit()
    conn.close()


class SettingsCommand(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _current(self, key: str):
        _label, env, _kind = SETTINGS[key]
        return getattr(config, env, None)

    def _describe(self, key: str) -> str:
        _label, _env, kind = SETTINGS[key]
        value = self._current(key)
        if not value:
            return "⚪ not set"
        if kind == "text":
            return f"`{value}`"
        guild = self.bot.get_guild(config.GUILD_ID) if config.GUILD_ID else None
        if kind == "server":
            g = self.bot.get_guild(value)
            return f"✅ **{g.name}** (`{value}`)" if g else f"❌ `{value}` (bot isn't in that server)"
        if guild is None:
            return f"`{value}`"
        obj = guild.get_role(value) if kind == "role" else guild.get_channel(value)
        return f"✅ {obj.mention} (`{value}`)" if obj else f"❌ `{value}` (not found in the server)"

    @app_commands.command(name="set", description="[Setup] Set the bot's server ID, roles and channels (saved to .env)")
    @app_commands.describe(
        setting="Which ID to change (leave empty to see them all)",
        value="The ID, or a #channel / @role mention. Type clear to unset it.",
    )
    @app_commands.choices(setting=CHOICES)
    async def set_cmd(self, interaction: discord.Interaction, setting: app_commands.Choice[str] = None,
                      value: str = None):
        if not permissions.can_set(interaction.user.id):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)

        if setting is None:
            lines = [f"**{label}** — {self._describe(key)}" for key, (label, _e, _k) in SETTINGS.items()]
            embed = discord.Embed(title="⚙️ Current settings", description="\n".join(lines)[:4000],
                                  color=discord.Color.blurple())
            embed.set_footer(text="Change one with /set setting:<name> value:<id>")
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        key = setting.value
        label, env, kind = SETTINGS[key]
        if value is None:
            return await interaction.response.send_message(
                f"**{label}** is currently {self._describe(key)}\nGive a `value` to change it.", ephemeral=True)

        value = value.strip()
        clearing = value.lower() in CLEAR_WORDS

        if kind == "text":
            if not clearing and not value.lower().startswith(("http://", "https://")):
                return await interaction.response.send_message("The link must start with http:// or https://", ephemeral=True)
            new = "" if clearing else value
        else:
            new_id = None if clearing else extract_id(value)
            if not clearing and new_id is None:
                return await interaction.response.send_message(
                    "That doesn't look like an ID - give the number, or a #channel / @role mention.", ephemeral=True)
            if new_id is not None:
                # Check it exists, so a typo doesn't silently break a feature.
                if kind == "server":
                    if self.bot.get_guild(new_id) is None:
                        return await interaction.response.send_message(
                            "I'm not in a server with that ID - invite the bot there first.", ephemeral=True)
                else:
                    guild = self.bot.get_guild(config.GUILD_ID) if config.GUILD_ID else interaction.guild
                    if guild is None:
                        return await interaction.response.send_message(
                            "Set the Server ID first (`/set setting:Server ID`).", ephemeral=True)
                    if kind == "role":
                        found = guild.get_role(new_id)
                    elif kind == "category":
                        found = guild.get_channel(new_id)
                        if found is not None and not isinstance(found, discord.CategoryChannel):
                            return await interaction.response.send_message(
                                "That ID is a channel, not a category.", ephemeral=True)
                    else:
                        found = guild.get_channel(new_id)
                    if found is None:
                        return await interaction.response.send_message(
                            f"I can't find that {kind} in the server - check the ID.", ephemeral=True)
            new = "" if new_id is None else str(new_id)

        try:
            write_env(env, new)
        except OSError as e:
            logging.exception("Couldn't write .env")
            return await interaction.response.send_message(f"Couldn't write the .env file: {e}", ephemeral=True)

        # Apply to the running bot.
        if kind == "text":
            setattr(config, env, new)
        else:
            setattr(config, env, int(new) if new else None)
        os.environ[env] = new
        if kind not in ("server", "text"):
            clear_overrides(key)

        extra = ""
        if kind == "server":
            await interaction.response.defer(ephemeral=True)
            try:
                await self.bot.sync_commands()
                extra = "\nSlash commands re-synced to that server."
            except discord.HTTPException:
                extra = "\n⚠️ Couldn't re-sync commands - try `!sync`."
            return await interaction.followup.send(
                f"✅ **{label}** {'cleared' if not new else 'set to ' + self._describe(key)}.{extra}", ephemeral=True)

        await interaction.response.send_message(
            f"✅ **{label}** {'cleared' if not new else 'set to ' + self._describe(key)}. Saved to `.env`.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(SettingsCommand(bot))
