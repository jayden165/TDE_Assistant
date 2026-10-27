"""
Text commands with custom prefixes.

Owners register any prefixes they like with /prefix (e.g. "s!", "TDE!", "DE?"). A message that
starts with one runs the matching slash command, with the arguments typed after it:

    s! rules
    TDE! purge 20
    DE? role @someone @Member
    s! welcome #welcome

Commands run through the very same handlers as the slash versions, so every permission check
still applies. Because there's no ephemeral reply in a text message, "only you can see this"
replies are sent to the user's DMs (short ones appear in the channel and delete themselves).
Commands that open a pop-up form (/rules edit, /embed, /welcome edit_message:True,
/applications setup) need the slash version.

Moderation commands (cogs/punishments.py) take a user mention or raw ID, then the reason:

    s! ban 123456789012345678 advertising
    s! timeout 123456789012345678 1h spamming

"<prefix>help" lists the commands you can use (cogs/prefix_help.py); "<prefix>help ban" explains one.
Owners can add simple text commands with /customcmd (cogs/custom_commands.py); they run like any other.

/insmp (owners) opens a form to set a custom message. Anyone can then type "<prefix>insmp"
(e.g. "?insmp" or "s! insmp") and the bot replies with that message.
"""
import logging

import discord
from discord import app_commands
from discord.ext import commands

import command_perms
import config
import database as db
import permissions
import ratelimit
from cogs.custom_commands import get_custom, send_custom

OT = discord.AppCommandOptionType

SHORT_REPLY = 200      # ephemeral text up to this long is posted in the channel, then deleted
SHORT_REPLY_TTL = 12   # seconds

# Short forms for commands that now live inside a group, so "s! purge 20 spam" still works.
PREFIX_ALIASES = {"purge": "channel purge", "lock": "channel lock",
                  "slowmode": "channel slowmode", "cooldown": "channel slowmode"}

INSMP_NAME = "insmp"
INSMP_KEY = "insmp_message"
INSMP_MAX = 2000       # a plain Discord message can't be longer


# ---------------------------------------------------------------------------------------
# A stand-in for discord.Interaction, so the existing command handlers can run on a message
# ---------------------------------------------------------------------------------------

class _Response:
    def __init__(self, owner: "PrefixInteraction"):
        self._o = owner
        self._done = False

    def is_done(self) -> bool:
        return self._done

    async def defer(self, **_kw):
        self._done = True
        try:
            await self._o.channel.typing()
        except discord.HTTPException:
            pass

    async def send_message(self, content=None, **kw):
        self._done = True
        return await self._o._send(content, **kw)

    async def edit_message(self, **kw):
        self._done = True
        await self._o.edit_original_response(**kw)

    async def send_modal(self, modal):
        self._done = True
        await self._o._send(
            f"That command opens a pop-up form, so use the slash command `/{self._o.command.qualified_name}` for it.",
            ephemeral=True,
        )


class _Followup:
    def __init__(self, owner: "PrefixInteraction"):
        self._o = owner

    async def send(self, content=None, **kw):
        return await self._o._send(content, **kw)


class PrefixInteraction:
    is_prefix = True  # lets cogs tell a text command from a real slash interaction

    def __init__(self, ctx: commands.Context, command):
        self._ctx = ctx
        self._last = None
        self.client = ctx.bot
        self.message = ctx.message
        self.user = ctx.author
        self.guild = ctx.guild
        self.guild_id = ctx.guild.id if ctx.guild else None
        self.channel = ctx.channel
        self.channel_id = ctx.channel.id
        self.command = command
        self.data = {}
        self.response = _Response(self)
        self.followup = _Followup(self)

    async def _send(self, content=None, **kw):
        ephemeral = kw.pop("ephemeral", False)
        kw.pop("thinking", None)
        kw = {k: v for k, v in kw.items() if v is not None}
        if content is not None:
            kw["content"] = content

        if not ephemeral:
            self._last = await self.message.reply(mention_author=False, **kw)
            return self._last

        has_rich = any(k in kw for k in ("embed", "embeds", "file", "files"))
        text = kw.get("content") or ""
        if "view" in kw:
            # Buttons on a private panel can't be made private here - don't expose them.
            self._last = await self.message.reply(
                f"That one shows a private panel, so use the slash command `/{self.command.qualified_name}`.",
                mention_author=False, delete_after=SHORT_REPLY_TTL,
            )
            return self._last
        if not has_rich and len(text) <= SHORT_REPLY:
            self._last = await self.message.reply(text, mention_author=False, delete_after=SHORT_REPLY_TTL,
                                                  allowed_mentions=discord.AllowedMentions.none())
            return self._last
        try:
            self._last = await self.user.send(**kw)
        except discord.HTTPException:
            self._last = await self.message.reply(
                "I couldn't DM you the private reply - open your DMs, or use the slash command.",
                mention_author=False, delete_after=SHORT_REPLY_TTL,
            )
        return self._last

    async def edit_original_response(self, **kw):
        kw = {k: v for k, v in kw.items() if v is not None}
        kw.pop("thinking", None)
        if self._last is not None:
            await self._last.edit(**kw)
        return self._last

    async def original_response(self):
        return self._last


# ---------------------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------------------

_QUOTES = {'"': '"', "“": "”"}


def tokenize(text: str):
    """[(token, start_index)], honouring "double quoted" tokens."""
    out, i, n = [], 0, len(text)
    while i < n:
        if text[i].isspace():
            i += 1
            continue
        start = i
        if text[i] in _QUOTES:
            close = _QUOTES[text[i]]
            j = text.find(close, i + 1)
            if j != -1:
                out.append((text[i + 1:j], start))
                i = j + 1
                continue
        while i < n and not text[i].isspace():
            i += 1
        out.append((text[start:i], start))
    return out


class ArgError(Exception):
    pass


_TRUE = {"true", "yes", "y", "on", "1", "enable", "enabled"}
_FALSE = {"false", "no", "n", "off", "0", "disable", "disabled"}


async def convert(ctx: commands.Context, param: app_commands.Parameter, token: str):
    t = param.type
    if param.choices:
        for c in param.choices:
            if token.lower() in (str(c.name).lower(), str(c.value).lower()):
                return c
        raise ArgError(f"`{param.display_name}` must be one of: " + ", ".join(c.name for c in param.choices))
    try:
        if t is OT.string:
            if param.min_value is not None and len(token) < param.min_value:
                raise ArgError(f"`{param.display_name}` is too short.")
            if param.max_value is not None and len(token) > param.max_value:
                raise ArgError(f"`{param.display_name}` is too long (max {int(param.max_value)} characters).")
            return token
        if t in (OT.integer, OT.number):
            value = int(token) if t is OT.integer else float(token)
            if param.min_value is not None and value < param.min_value or \
               param.max_value is not None and value > param.max_value:
                raise ArgError(f"`{param.display_name}` must be between {param.min_value} and {param.max_value}.")
            return value
        if t is OT.boolean:
            if token.lower() in _TRUE:
                return True
            if token.lower() in _FALSE:
                return False
            raise ArgError(f"`{param.display_name}` must be yes or no.")
        if t is OT.user:
            try:
                return await commands.MemberConverter().convert(ctx, token)
            except commands.BadArgument:
                return await commands.UserConverter().convert(ctx, token)
        if t is OT.channel:
            return await commands.GuildChannelConverter().convert(ctx, token)
        if t is OT.role:
            return await commands.RoleConverter().convert(ctx, token)
        if t is OT.mentionable:
            try:
                return await commands.RoleConverter().convert(ctx, token)
            except commands.BadArgument:
                return await commands.MemberConverter().convert(ctx, token)
    except (ValueError, commands.BadArgument):
        raise ArgError(f"I couldn't understand `{token}` for `{param.display_name}`.")
    raise ArgError(f"`{param.display_name}` can't be given in a text command - use the slash command.")


def error_embed(text: str, title: str = "❌ Couldn't do that") -> discord.Embed:
    return discord.Embed(title=title, description=text, color=discord.Color.red())


def usage(prefix: str, cmd) -> str:
    parts = []
    for p in cmd.parameters:
        parts.append(f"<{p.display_name}>" if p.required else f"[{p.display_name}]")
    return f"{prefix}{cmd.qualified_name} " + " ".join(parts)


class InsmpModal(discord.ui.Modal, title="Set the ?insmp message"):
    message = discord.ui.TextInput(
        label="Message",
        style=discord.TextStyle.paragraph,
        max_length=INSMP_MAX,
        required=False,
        placeholder="What the bot says when someone types the prefix + insmp. Leave blank to clear it.",
    )

    def __init__(self, current: str | None):
        super().__init__()
        if current:
            self.message.default = current

    async def on_submit(self, interaction: discord.Interaction):
        text = str(self.message.value).strip()
        if text:
            db.set_setting(interaction.guild_id, INSMP_KEY, text)
            prefixes = permissions.get_prefixes()
            hint = (f"Anyone can now type `{prefixes[0]}{INSMP_NAME}` to get it." if prefixes
                    else "Add a prefix with `/prefix action:Add prefix:?` so people can trigger it with `?insmp`.")
            await interaction.response.send_message(f"✅ Message saved. {hint}", ephemeral=True)
        else:
            db.delete_setting(interaction.guild_id, INSMP_KEY)
            await interaction.response.send_message("🗑️ Message cleared.", ephemeral=True)


class PrefixCommands(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---------------- /prefix (owners) ----------------

    @app_commands.command(name="prefix", description="[Owner] Set the prefixes that run commands as text (e.g. s! rules)")
    @app_commands.describe(action="Add, remove or list prefixes", prefix="The prefix, e.g. s! or TDE! or DE?")
    @app_commands.choices(action=[
        app_commands.Choice(name="Add", value="add"),
        app_commands.Choice(name="Remove", value="remove"),
        app_commands.Choice(name="List", value="list"),
    ])
    async def prefix(self, interaction: discord.Interaction, action: app_commands.Choice[str], prefix: str = None):
        if not permissions.is_owner(interaction.user.id):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)

        if action.value == "list":
            prefixes = permissions.get_prefixes()
            body = ("\n".join(f"`{p}`" for p in prefixes) if prefixes
                    else "No prefixes set. Add one with `/prefix action:Add prefix:s!`.")
            return await interaction.response.send_message(
                embed=discord.Embed(title="⌨️ Command prefixes", description=body, color=discord.Color.blurple()),
                ephemeral=True,
            )

        prefix = (prefix or "").strip()
        if not prefix:
            return await interaction.response.send_message("Give a prefix.", ephemeral=True)
        if len(prefix) > permissions.MAX_PREFIX_LENGTH:
            return await interaction.response.send_message(
                f"Prefixes can be at most {permissions.MAX_PREFIX_LENGTH} characters.", ephemeral=True)

        if action.value == "add":
            msg = (f"✅ Added `{prefix}` - try `{prefix} rules`." if permissions.add_prefix(prefix)
                   else f"`{prefix}` is already a prefix.")
        else:
            msg = (f"🗑️ Removed `{prefix}`." if permissions.remove_prefix(prefix)
                   else f"`{prefix}` isn't a prefix.")
        await interaction.response.send_message(msg, ephemeral=True)

    # ---------------- /insmp (owners) ----------------

    @app_commands.command(name="insmp", description="[Owner] Set the message the bot sends when someone types <prefix>insmp")
    async def insmp(self, interaction: discord.Interaction):
        if not permissions.is_owner(interaction.user.id):
            return await interaction.response.send_message(command_perms.NO_PERMS_MESSAGE, ephemeral=True)
        if interaction.guild is None:
            return await interaction.response.send_message("Use this in the server.", ephemeral=True)
        current = db.get_setting(interaction.guild.id, INSMP_KEY)
        await interaction.response.send_modal(InsmpModal(current))

    async def send_insmp(self, message: discord.Message):
        text = db.get_setting(message.guild.id, INSMP_KEY)
        if not text:
            return await message.reply("That message hasn't been set up yet.", mention_author=False,
                                       delete_after=SHORT_REPLY_TTL)
        no_pings = discord.AllowedMentions(everyone=False, roles=False)
        try:
            await message.reply(text, mention_author=False, allowed_mentions=no_pings)
        except discord.HTTPException:
            try:  # e.g. the trigger message was deleted already
                await message.channel.send(text, allowed_mentions=no_pings)
            except discord.HTTPException:
                pass

    # ---------------- message handling ----------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None or not message.content:
            return
        if config.GUILD_ID and message.guild.id != config.GUILD_ID:
            return
        content = message.content
        for prefix in sorted(permissions.get_prefixes(), key=len, reverse=True):
            if content.lower().startswith(prefix.lower()):
                rest = content[len(prefix):].strip()
                if rest:
                    await self.run(message, prefix, rest)
                return

    async def _slowed_down(self, message: discord.Message) -> bool:
        """Normal members get a command rate limit (staff are exempt). True = block this command."""
        wait = ratelimit.check(message.author)
        if wait is None:
            return False
        await message.reply(embed=discord.Embed(description=ratelimit.notice(wait), color=discord.Color.orange()),
                            mention_author=False, delete_after=8)
        return True

    async def run(self, message: discord.Message, prefix: str, text: str):
        head = text.split(None, 1)
        if head and head[0].lower() in PREFIX_ALIASES and self.bot.tree.get_command(head[0].lower()) is None:
            text = PREFIX_ALIASES[head[0].lower()] + (" " + head[1] if len(head) > 1 else "")
        tokens = tokenize(text)
        first = tokens[0][0].lower()
        if first in ("help", "commands"):
            if await self._slowed_down(message):
                return
            helper = self.bot.get_cog("PrefixHelp")
            if helper is not None:
                return await helper.show(message, prefix, [t for t, _ in tokens[1:]])
        if first == INSMP_NAME:
            # Open to everyone: typing <prefix>insmp posts the message an owner saved with /insmp.
            if await self._slowed_down(message):
                return
            return await self.send_insmp(message)
        cmd = self.bot.tree.get_command(first)
        if cmd is None:
            custom = get_custom(message.guild.id, first)
            if custom is not None:  # a custom command made with /customcmd
                if await self._slowed_down(message):
                    return
                args = text[tokens[1][1]:].strip() if len(tokens) > 1 else ""
                return await send_custom(message, custom, args)
            return  # not one of our commands - ignore, it's just chat
        idx = 1
        while isinstance(cmd, app_commands.Group):
            sub = cmd.get_command(tokens[idx][0].lower()) if idx < len(tokens) else None
            if sub is None:
                names = " | ".join(c.name for c in cmd.commands)
                return await message.reply(
                    embed=error_embed(f"`{prefix}{cmd.qualified_name} <{names}>`", "ℹ️ Usage"), mention_author=False)
            cmd, idx = sub, idx + 1

        decision = command_perms.evaluate(message.author, message.guild.id, cmd.qualified_name)
        if decision == command_perms.DENY:
            return await message.reply(embed=error_embed(command_perms.NO_PERMS_MESSAGE), mention_author=False)
        if await self._slowed_down(message):
            return

        ctx = await self.bot.get_context(message)
        kwargs = {}
        params = cmd.parameters
        try:
            for n, param in enumerate(params):
                if idx >= len(tokens):
                    if param.required:
                        raise ArgError(f"`{usage(prefix, cmd)}`")
                    break
                if param.type is OT.attachment:
                    if not message.attachments:
                        if param.required:
                            raise ArgError("Attach a file to your message for that.")
                        continue
                    kwargs[param.name] = message.attachments[0]
                    continue
                token, start = tokens[idx]
                last = n == len(params) - 1
                if last and param.type is OT.string and not param.choices:
                    token = text[start:].strip().strip('"')  # the last text argument takes the rest
                    idx = len(tokens)
                else:
                    idx += 1
                kwargs[param.name] = await convert(ctx, param, token)
            # attachment params are optional and not positional - pick them up if still unset
            for param in params:
                if param.type is OT.attachment and param.name not in kwargs and message.attachments:
                    kwargs[param.name] = message.attachments[0]
            for param in params:
                if param.required and param.name not in kwargs:
                    raise ArgError(f"`{usage(prefix, cmd)}`")
        except ArgError as e:
            return await message.reply(embed=error_embed(str(e), "ℹ️ Usage"), mention_author=False)

        shim = PrefixInteraction(ctx, cmd)
        token = command_perms.set_granted(decision == command_perms.ALLOW)
        try:
            binding = getattr(cmd, "binding", None)
            node = cmd
            while binding is None and getattr(node, "parent", None) is not None:
                node = node.parent
                binding = getattr(node, "binding", None)
            if binding is not None:
                await cmd.callback(binding, shim, **kwargs)
            else:
                await cmd.callback(shim, **kwargs)
        except Exception:
            logging.exception(f"Text command failed: {message.content!r}")
            await message.reply(embed=error_embed("Something went wrong running that command."), mention_author=False)
        finally:
            command_perms.reset_granted(token)


async def setup(bot: commands.Bot):
    await bot.add_cog(PrefixCommands(bot))
