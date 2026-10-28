"""
/set - lets the setup user (permissions.SETUP_USER_ID) change the bot's IDs from Discord instead of
editing .env by hand: the server ID, staff/admin/mute roles, every log/ticket/application channel,
and the ticket categories.

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

_BOT_DIR = os.path.dirname(os.path.abspath(config.__file__))
ENV_PATH = os.path.join(_BOT_DIR, ".env")


def find_env_path() -> str:
    """The env file /set should write to. Order: the ENV_FILE variable if you set it, then a writable
    .env / tde.env in the bot folder or up to two folders above it (so a tde.env that sits beside the
    releases/ folder on the host is found), then the plain .env next to the code."""
    explicit = os.environ.get("ENV_FILE")
    if explicit:
        return explicit
    folders = [_BOT_DIR, os.path.dirname(_BOT_DIR), os.path.dirname(os.path.dirname(_BOT_DIR))]
    existing = [os.path.join(d, n) for d in folders for n in ("tde.env", ".env") if os.path.isfile(os.path.join(d, n))]
    for path in existing:
        if os.access(path, os.W_OK):
            return path
    return existing[0] if existing else ENV_PATH

# setting key -> (label, env var name, kind)   kind: server | channel | role | category | text
SETTINGS: dict[str, tuple[str, str, str]] = {"server-id": ("Server ID", "GUILD_ID", "server")}
for _key, (_label, _attr, *_rest) in gc.CHANNEL_SETTINGS.items():
    SETTINGS[_key] = (_label, _attr, "channel")
for _key, (_label, _attr) in gc.ROLE_SETTINGS.items():
    SETTINGS[_key] = (_label, _attr, "role")
for _key, (_label, _attr) in gc.CATEGORY_SETTINGS.items():
    SETTINGS[_key] = (_label, _attr, "category")

CHOICES = [app_commands.Choice(name=label[:100], value=key) for key, (label, _e, _k) in SETTINGS.items()]
CLEAR_WORDS = {"clear", "none", "reset", "remove", "0"}


def write_env(name: str, value: str):
    """Sets NAME=value in the .env file (replacing the line if it exists), atomically."""
    path = find_env_path()
    lines = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    pattern = re.compile(r"^\s*" + re.escape(name) + r"\s*=")
    new_line = f"{name}={value}"
    for i, line in enumerate(lines):
        if pattern.match(line):
            lines[i] = new_line
            break
    else:
        lines.append(new_line)
    text = "\n".join(lines) + "\n"
    if os.access(os.path.dirname(path) or ".", os.W_OK):
        tmp = path + ".tmp"  # atomic: write beside it, then swap
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    else:  # the folder is locked but the file itself may be writable (e.g. a mounted env file)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)


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
        _label, env, kind = SETTINGS[key]
        if kind in ("channel", "role", "category") and config.GUILD_ID:
            return gc.get(config.GUILD_ID, key) or None  # includes choices made with /setchannel or /log-aio
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

        env_saved = True
        try:
            write_env(env, new)
        except OSError as e:
            # e.g. a read-only release folder on the host. Channel/role/category settings can live in the
            # bot's database instead (it takes priority over .env), so they still save and survive restarts.
            logging.warning("Couldn't write .env (%s) - saving %s to the database instead", e, key)
            env_saved = False
            if kind in ("server", "text"):
                return await interaction.response.send_message(
                    f"Couldn't write the .env file ({e}). This one can only be saved there - the folder is "
                    "read-only on this host, so change it in your host's environment settings instead.", ephemeral=True)
            target_guild = interaction.guild or self.bot.get_guild(config.GUILD_ID or 0)
            if target_guild is None:
                return await interaction.response.send_message(
                    f"Couldn't write the .env file ({e}), and I couldn't tell which server to save this for.", ephemeral=True)
            gc.set_override(target_guild.id, key, new or "0")  # "0" = cleared (beats the .env default)

        # Apply to the running bot.
        if kind == "text":
            setattr(config, env, new)
        else:
            setattr(config, env, int(new) if new else None)
        os.environ[env] = new
        if kind not in ("server", "text") and env_saved:
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
            f"✅ **{label}** {'cleared' if not new else 'set to ' + self._describe(key)}. {'Saved to the env file.' if env_saved else 'Saved to the bot database (.env is read-only here), so it survives restarts.'}",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(SettingsCommand(bot))
