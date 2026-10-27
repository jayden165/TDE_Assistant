"""
Custom commands and command requests (bot owners only).

/customcmd add | edit | remove | list
    Owners create simple text commands without touching code. `add`/`edit` open a pop-up form for the
    name and the response. Anyone can then run it with a prefix, e.g. "?ip" or "M! discord". The
    response can be plain text or an embed, and may use:
        {user}  mentions whoever ran it        {username}  their name
        {server} the server name               {args}      anything typed after the command
    A custom command can't reuse the name of a built-in command. @everyone/role pings are suppressed.
    These are text replies only - they can't run code or moderation actions.

/customcmd request
    For things a custom command can't do (a real new feature). Opens a form - command name, what it
    should do, why, who should use it / its arguments, extra info - and DMs it to the developer with the
    requester, server and channel attached. If the DM can't be delivered it is posted in the staff action
    log channel instead. The developer is DEV_USER_ID in .env (defaults to the setup user in permissions.py).
"""
import json
import logging
import re
import time

import discord
from discord import app_commands
from discord.ext import commands

import command_perms
import config
import database as db
import guild_config as gc
import permissions
from permissions import is_owner

KEY_PREFIX = "customcmd:"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
RESERVED = {"help", "commands", "insmp"}  # handled directly by the prefix system
MAX_COMMANDS = 100
PLAIN_MAX = 2000
_NO_PINGS = discord.AllowedMentions(everyone=False, roles=False)


# ---------------- storage ----------------

def get_custom(guild_id: int, name: str) -> dict | None:
    raw = db.get_setting(guild_id, KEY_PREFIX + name.lower())
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def all_custom(guild_id: int) -> dict[str, dict]:
    out = {}
    for row in db.get_settings_by_prefix(guild_id, KEY_PREFIX):
        try:
            out[row["key"][len(KEY_PREFIX):]] = json.loads(row["value"])
        except (ValueError, TypeError):
            continue
    return dict(sorted(out.items()))


def _save(guild_id: int, name: str, entry: dict):
    db.set_setting(guild_id, KEY_PREFIX + name, json.dumps(entry))


def render(text: str, message: discord.Message, args: str) -> str:
    return (text.replace("{user}", message.author.mention)
                .replace("{username}", message.author.display_name)
                .replace("{server}", message.guild.name)
                .replace("{args}", args))


async def send_custom(message: discord.Message, entry: dict, args: str = ""):
    """Posts a custom command's response as a reply to the message that triggered it."""
    body = render(entry.get("content", ""), message, args)[:4000 if entry.get("embed") else PLAIN_MAX]
    embed = None
    try:
        if entry.get("embed"):
            embed = discord.Embed(description=body, color=discord.Color.blurple())
            if entry.get("title"):
                embed.title = render(entry["title"], message, args)[:256]
            await message.reply(embed=embed, mention_author=False, allowed_mentions=_NO_PINGS)
        else:
            await message.reply(body or "\u200b", mention_author=False, allowed_mentions=_NO_PINGS)
    except discord.HTTPException:
        try:  # e.g. the triggering message was deleted already
            if entry.get("embed"):
                await message.channel.send(embed=embed, allowed_mentions=_NO_PINGS)
            else:
                await message.channel.send(body or "\u200b", allowed_mentions=_NO_PINGS)
        except discord.HTTPException:
            pass


# ---------------- modals ----------------

class CustomCommandModal(discord.ui.Modal):
    def __init__(self, cog: "CustomCommands", name: str | None = None, existing: dict | None = None):
        super().__init__(title=f"Edit {name}" if name else "New custom command")
        self.cog = cog
        self.fixed_name = name
        existing = existing or {}
        self.name_in = None
        if name is None:
            self.name_in = discord.ui.TextInput(label="Command name", max_length=32,
                                                placeholder="e.g. ip  (people type ?ip)")
            self.add_item(self.name_in)
        self.body_in = discord.ui.TextInput(
            label="Response", style=discord.TextStyle.paragraph, max_length=4000,
            default=existing.get("content"), placeholder="{user} {username} {server} {args} work here")
        self.title_in = discord.ui.TextInput(label="Embed title (optional)", max_length=256, required=False,
                                             default=existing.get("title"))
        self.embed_in = discord.ui.TextInput(
            label="Send as an embed? (yes / no)", max_length=3, required=False,
            default="yes" if existing.get("embed") else "no")
        for item in (self.body_in, self.title_in, self.embed_in):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        name = self.fixed_name or str(self.name_in.value).strip().lower().lstrip("/!?")
        body = str(self.body_in.value).strip()
        title = str(self.title_in.value).strip()
        as_embed = str(self.embed_in.value).strip().lower() in ("yes", "y", "true", "1") or bool(title)
        gid = interaction.guild.id

        async def fail(text: str):
            await interaction.response.send_message(
                embed=discord.Embed(title="❌ Couldn't save it", description=text, color=discord.Color.red()),
                ephemeral=True)

        if self.fixed_name is None:
            if not NAME_RE.match(name):
                return await fail("Names are 1-32 characters: lowercase letters, numbers, `-` and `_`.")
            if name in RESERVED or self.cog.bot.tree.get_command(name) is not None:
                return await fail(f"`{name}` is already a built-in command - pick another name.")
            if get_custom(gid, name) is not None:
                return await fail(f"`{name}` already exists - use `/customcmd edit`.")
            if len(all_custom(gid)) >= MAX_COMMANDS:
                return await fail(f"You can have at most {MAX_COMMANDS} custom commands.")
        if not body:
            return await fail("The response can't be empty.")
        if not as_embed and len(body) > PLAIN_MAX:
            return await fail(f"Plain messages max out at {PLAIN_MAX} characters - shorten it, or send it as an embed.")

        old = get_custom(gid, name) or {}
        _save(gid, name, {
            "content": body, "title": title, "embed": as_embed,
            "created_by": old.get("created_by", interaction.user.id), "edited_by": interaction.user.id,
            "updated_at": time.time(),
        })
        logging.info("custom command %r %s by %s", name, "edited" if old else "created", interaction.user.id)
        prefixes = permissions.get_prefixes()
        hint = (f"People can now type `{prefixes[0]}{name}`." if prefixes
                else "Add a prefix with `/prefix action:Add prefix:?` so people can use it.")
        await interaction.response.send_message(
            f"✅ Custom command `{name}` {'updated' if old else 'created'}. {hint}", ephemeral=True)


class RequestModal(discord.ui.Modal, title="Request a new command"):
    name_in = discord.ui.TextInput(label="Command name", max_length=60, placeholder="e.g. /giveaway")
    what_in = discord.ui.TextInput(label="What should it do?", style=discord.TextStyle.paragraph, max_length=1000)
    why_in = discord.ui.TextInput(label="Reason - why do we need it?", style=discord.TextStyle.paragraph,
                                  max_length=1000)
    usage_in = discord.ui.TextInput(
        label="Who can use it + what options does it take?", style=discord.TextStyle.paragraph, max_length=500,
        placeholder="e.g. staff only; options: user, duration, reason")
    extra_in = discord.ui.TextInput(label="Anything else? (optional)", style=discord.TextStyle.paragraph,
                                    max_length=1000, required=False)

    def __init__(self, cog: "CustomCommands"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.deliver_request(interaction, {
            "Command": str(self.name_in.value), "What it should do": str(self.what_in.value),
            "Reason": str(self.why_in.value), "Who can use it / options": str(self.usage_in.value),
            "Extra info": str(self.extra_in.value) or "-",
        })


# ---------------- cog ----------------

class CustomCommands(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @staticmethod
    def _owner_only(interaction: discord.Interaction) -> bool:
        return is_owner(interaction.user.id) and interaction.guild is not None

    customcmd_group = app_commands.Group(name="customcmd", description="[Owner] Create custom text commands, or request a new feature")

    @customcmd_group.command(name="add", description="Create a custom command (opens a form)")
    async def cc_add(self, interaction: discord.Interaction):
        if not self._owner_only(interaction):
            return await interaction.response.send_message(command_perms.NO_PERMS_MESSAGE, ephemeral=True)
        await interaction.response.send_modal(CustomCommandModal(self))

    @customcmd_group.command(name="edit", description="Edit a custom command (opens a form)")
    @app_commands.describe(name="The custom command to edit")
    async def cc_edit(self, interaction: discord.Interaction, name: str):
        if not self._owner_only(interaction):
            return await interaction.response.send_message(command_perms.NO_PERMS_MESSAGE, ephemeral=True)
        name = name.lower().strip()
        entry = get_custom(interaction.guild.id, name)
        if entry is None:
            return await interaction.response.send_message(f"There's no custom command called `{name}`.", ephemeral=True)
        await interaction.response.send_modal(CustomCommandModal(self, name, entry))

    @customcmd_group.command(name="remove", description="Delete a custom command")
    @app_commands.describe(name="The custom command to delete")
    async def cc_remove(self, interaction: discord.Interaction, name: str):
        if not self._owner_only(interaction):
            return await interaction.response.send_message(command_perms.NO_PERMS_MESSAGE, ephemeral=True)
        name = name.lower().strip()
        if get_custom(interaction.guild.id, name) is None:
            return await interaction.response.send_message(f"There's no custom command called `{name}`.", ephemeral=True)
        db.delete_setting(interaction.guild.id, KEY_PREFIX + name)
        logging.info("custom command %r removed by %s", name, interaction.user.id)
        await interaction.response.send_message(f"🗑️ Removed `{name}`.", ephemeral=True)

    @customcmd_group.command(name="list", description="List the custom commands")
    async def cc_list(self, interaction: discord.Interaction):
        if not self._owner_only(interaction):
            return await interaction.response.send_message(command_perms.NO_PERMS_MESSAGE, ephemeral=True)
        entries = all_custom(interaction.guild.id)
        if not entries:
            body = "None yet - create one with `/customcmd add`."
        else:
            lines = []
            for name, e in entries.items():
                preview = (e.get("content", "")[:60] + ("…" if len(e.get("content", "")) > 60 else "")).replace("\n", " ")
                lines.append(f"`{name}` {'(embed) ' if e.get('embed') else ''}- {preview}")
            body = "\n".join(lines)[:4000]
        await interaction.response.send_message(
            embed=discord.Embed(title="🧩 Custom commands", description=body, color=discord.Color.blurple()),
            ephemeral=True)

    @cc_edit.autocomplete("name")
    @cc_remove.autocomplete("name")
    async def _name_autocomplete(self, interaction: discord.Interaction, current: str):
        if not is_owner(interaction.user.id) or interaction.guild is None:
            return []
        current = current.lower()
        return [app_commands.Choice(name=n, value=n) for n in all_custom(interaction.guild.id) if current in n][:25]

    # ---------------- /customcmd request ----------------

    @customcmd_group.command(name="request", description="Ask the developer for a new feature (sends them the details)")
    async def cc_request(self, interaction: discord.Interaction):
        if not is_owner(interaction.user.id):
            return await interaction.response.send_message(command_perms.NO_PERMS_MESSAGE, ephemeral=True)
        await interaction.response.send_modal(RequestModal(self))

    async def deliver_request(self, interaction: discord.Interaction, fields: dict[str, str]):
        number = int(db.get_setting(0, "cmd_request_counter", "0") or 0) + 1
        db.set_setting(0, "cmd_request_counter", number)
        user = interaction.user
        embed = discord.Embed(title=f"📝 Command request #{number}", color=discord.Color.blurple(),
                              timestamp=discord.utils.utcnow())
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name="Requested by", value=f"{user.mention}\n`{user}` ({user.id})", inline=True)
        where = f"{interaction.guild.name} (`{interaction.guild.id}`)" if interaction.guild else "DMs"
        if interaction.channel is not None and interaction.guild is not None:
            where += f"\n{interaction.channel.mention}"
        embed.add_field(name="Server", value=where, inline=True)
        for name, value in fields.items():
            embed.add_field(name=name, value=value[:1024] or "-", inline=False)
        logging.info("command request #%s from %s: %s", number, user.id, fields.get("Command"))

        dev_id = config.DEV_USER_ID or permissions.SETUP_USER_ID
        delivered = False
        try:
            dev = await self.bot.fetch_user(dev_id)
            await dev.send(embed=embed)
            delivered = True
        except discord.HTTPException:
            logging.warning("couldn't DM command request #%s to %s", number, dev_id)

        if not delivered and interaction.guild is not None:
            channel = gc.get_log_channel(interaction.guild, "staff-action-log-channel")
            if channel is not None:
                try:
                    await channel.send(content=f"<@{dev_id}> I couldn't DM you this command request:", embed=embed,
                                       allowed_mentions=discord.AllowedMentions(users=True))
                    delivered = True
                except discord.HTTPException:
                    pass

        if delivered:
            text = f"✅ Request **#{number}** sent to the developer."
            color = discord.Color.green()
        else:
            text = (f"⚠️ Request **#{number}** was saved in the bot's log, but I couldn't DM the developer "
                    "(their DMs may be closed). Tell them directly.")
            color = discord.Color.orange()
        await interaction.response.send_message(embed=discord.Embed(description=text, color=color), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(CustomCommands(bot))
