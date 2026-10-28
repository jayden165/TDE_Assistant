"""
Help for text (prefix) commands:

    s! help              every command you're allowed to use, grouped, with how to type it
    s! help ban          details for one command: usage, each option, an example
    s! help role add     works for subcommands too (and for custom commands)

Only commands the person can actually run are shown (same rules as /help). Commands that need a
pop-up form or a private panel are marked "slash only". Replies are embeds posted as a reply.
"""
import discord
from discord import app_commands
from discord.ext import commands

import database as db
import permissions
from cogs.custom_commands import all_custom, get_custom

OT = discord.AppCommandOptionType

# These open a pop-up form or a private panel, so they only work as slash commands.
SLASH_ONLY = {"embed", "moderation-panel", "applications setup", "insmp", "customcmd set", "customcmd request",
              "recording-post", "recording-end"}

CATEGORIES = [
    ("🛡️ Moderation", ["ban", "unban", "kick", "timeout", "mute", "warn", "infractions", "moderation-panel", "role", "channel"]),
    ("📬 Support & applications", ["panel", "ticket", "support-mode", "modmail", "applications"]),
    ("🎬 Recordings", ["recording-post", "recording-end", "recording-ban"]),
    ("💬 Messages", ["say", "dm", "embed", "insmp", "customcmd"]),
    ("🏠 Server", ["welcome", "upload"]),
    ("📺 YouTube", ["upload"]),
    ("🔒 Security", ["honeypot", "antiraid"]),
    ("⚙️ Bot", ["ping", "status", "debug", "log-aio", "setchannel", "prefix", "owner", "commandperms", "set"]),
]
CATEGORY_OF = {name: title for title, names in CATEGORIES for name in names}

EXAMPLES = {
    "ban": "{p} ban 123456789012345678 advertising in DMs",
    "unban": "{p} unban 123456789012345678 appealed",
    "kick": "{p} kick @someone being rude",
    "timeout": "{p} timeout 123456789012345678 1h spamming   (use off instead of 1h to end it)",
    "warn": "{p} warn 123456789012345678 last warning about links",
    "mute": "{p} mute 123456789012345678 spam   (run it again to unmute)",
    "role": "{p} role @someone @Member   (add yes at the end to take the role away)",
    "channel purge": "{p} purge 20 cleaning up",
    "channel lock": "{p} lock 1h raid in progress   (use off instead of 1h to unlock)",
    "welcome": "{p} welcome #welcome   (no options = preview the welcome message)",
    "ticket close": "{p} ticket close resolved - thanks!",
    "ticket member": "{p} ticket member @someone   (add yes at the end to remove them)",
    "recording-ban": "{p} recording-ban 123456789012345678 no-show   (reason: lift = allow again)",
    "status": "{p} status watching over the server   (no options = list them)",
    "upload channel set": "{p} upload channel set #youtube-updates",
    "upload channel set": "{p} upload channel set #uploads",
    "log-aio": "{p} log-aio single",
    "support-mode": "{p} support-mode modmail",
    "modmail close": "{p} modmail close issue resolved",
    "modmail block": "{p} modmail block 123456789012345678 spamming   (run it again to unblock; no member = blocklist)",
    "modmail contact": "{p} modmail contact 123456789012345678 Hi! Can we chat about your report?",
    "setchannel": "{p} setchannel ticket-panel #tickets",
}

TYPE_LABEL = {
    OT.string: "text", OT.integer: "whole number", OT.number: "number", OT.boolean: "yes / no",
    OT.user: "@mention or user ID", OT.channel: "#channel", OT.role: "@role", OT.mentionable: "@mention",
    OT.attachment: "attached file",
}


def describe(cmd) -> str:
    return (cmd.description or "").split("] ", 1)[-1]


def short_usage(cmd) -> str:
    if isinstance(cmd, app_commands.Group):  # a sub-group, e.g. "upload channel <set|remove>"
        return f"{cmd.qualified_name} <{'|'.join(c.name for c in cmd.commands)}>"
    parts, optional = [], 0
    for p in cmd.parameters:
        if p.required:
            parts.append(f"<{p.display_name}>")
        elif optional < 2:
            parts.append(f"[{p.display_name}]")
            optional += 1
        else:
            parts.append("[...]")
            break
    return " ".join([cmd.qualified_name] + parts)


def full_usage(cmd) -> str:
    return " ".join([cmd.qualified_name] + [f"<{p.display_name}>" if p.required else f"[{p.display_name}]"
                                            for p in cmd.parameters])


class PrefixHelp(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _visible(self, member: discord.Member, cmd) -> bool:
        if cmd.qualified_name == "set":
            return permissions.can_set(member.id)
        util = self.bot.get_cog("Utility")
        return util._can_see(member, cmd.description or "", cmd.qualified_name) if util else True

    def _resolve(self, tokens: list[str]):
        cmd = self.bot.tree.get_command(tokens[0].lower())
        i = 1
        while isinstance(cmd, app_commands.Group) and i < len(tokens):
            sub = cmd.get_command(tokens[i].lower())
            if sub is None:
                break
            cmd, i = sub, i + 1
        return cmd

    @staticmethod
    def _tag(cmd) -> str:
        return " *(slash only)*" if cmd.qualified_name in SLASH_ONLY else ""

    # ---------------- overview ----------------

    async def show(self, message: discord.Message, prefix: str, args: list[str]):
        if args:
            return await self._show_one(message, prefix, args)
        member, p = message.author, prefix
        sections: dict[str, list[str]] = {}
        for top in sorted(self.bot.tree.get_commands(), key=lambda c: c.name):
            if top.name == "help" or not self._visible(member, top):
                continue
            if isinstance(top, app_commands.Group):
                subs = [c for c in top.commands if self._visible(member, c)]
                if not subs:
                    continue
                line = f"`{p} {top.name} <{'|'.join(c.name for c in subs)}>` - {describe(top)}"
            else:
                line = f"`{p} {short_usage(top)}` - {describe(top)}{self._tag(top)}"
            sections.setdefault(CATEGORY_OF.get(top.name, "📦 Other"), []).append(line)

        embeds = [self._new_embed(p, first=True)]

        def add_field(name: str, lines: list[str]):
            chunk, size, n = [], 0, 0
            for line in lines + [None]:
                if line is None or size + len(line) + 1 > 1000:
                    if chunk:
                        title = name if n == 0 else f"{name} (cont.)"
                        value = "\n".join(chunk)
                        if sum(len(f.value) + len(f.name) for f in embeds[-1].fields) + len(value) > 4500:
                            embeds.append(self._new_embed(p))
                        embeds[-1].add_field(name=title, value=value, inline=False)
                        n += 1
                    chunk, size = [], 0
                if line is not None:
                    chunk.append(line)
                    size += len(line) + 1

        for title, _names in CATEGORIES:
            if title in sections:
                add_field(title, sections.pop(title))
        for title, lines in sections.items():
            add_field(title, lines)

        extras = [f"`{p} help [command]` - this list, or details for one command"]
        if db.get_setting(message.guild.id, "insmp_message"):
            extras.append(f"`{p}insmp` - shows the server's saved message")
        for name in all_custom(message.guild.id):
            extras.append(f"`{p} {name}` - custom command")
        add_field("🧩 Other / custom commands", extras)

        embeds[-1].set_footer(text="<required>  [optional]  -  slash only = needs the / version")
        await message.reply(embed=embeds[0], mention_author=False)
        for e in embeds[1:]:
            await message.channel.send(embed=e)

    def _new_embed(self, prefix: str, first: bool = False) -> discord.Embed:
        embed = discord.Embed(color=discord.Color.blurple())
        if first:
            embed.title = "⌨️ Text commands"
            prefixes = ", ".join(f"`{x}`" for x in permissions.get_prefixes()) or f"`{prefix}`"
            embed.description = (f"Type a prefix, then the command: `{prefix} <command> ...`\n"
                                 f"Prefixes: {prefixes}\nOnly commands you can use are listed.")
        return embed

    # ---------------- one command ----------------

    async def _show_one(self, message: discord.Message, prefix: str, args: list[str]):
        p = prefix
        name = args[0].lower()
        custom = get_custom(message.guild.id, name)
        cmd = self._resolve(args)
        if cmd is None or not self._visible(message.author, cmd):
            if custom:
                embed = discord.Embed(title=f"⌨️ {p} {name}", color=discord.Color.blurple(),
                                      description="A custom command - it posts a saved message.")
                embed.add_field(name="Usage", value=f"`{p} {name}`", inline=False)
                return await message.reply(embed=embed, mention_author=False)
            embed = discord.Embed(title="❌ Unknown command", color=discord.Color.red(),
                                  description=f"I don't know a command you can use called `{' '.join(args)}`. "
                                              f"Try `{p} help`.")
            return await message.reply(embed=embed, mention_author=False)

        embed = discord.Embed(title=f"⌨️ {p} {cmd.qualified_name}", description=describe(cmd) or None,
                              color=discord.Color.blurple())
        if isinstance(cmd, app_commands.Group):
            subs = [c for c in cmd.commands if self._visible(message.author, c)]
            embed.add_field(name="Subcommands", inline=False, value="\n".join(
                f"`{p} {short_usage(c)}` - {describe(c)}{self._tag(c)}" for c in subs)[:1024] or "-")
        else:
            embed.add_field(name="Usage", value=f"`{p} {full_usage(cmd)}`", inline=False)
            lines = []
            for prm in cmd.parameters:
                kind = TYPE_LABEL.get(prm.type, "value")
                if prm.choices:
                    kind = " / ".join(c.name for c in prm.choices)
                note = f" - {prm.description}" if prm.description and prm.description != "…" else ""
                lines.append(f"`{prm.display_name}` ({'required' if prm.required else 'optional'}, {kind}){note}")
            if lines:
                embed.add_field(name="Options", value="\n".join(lines)[:1024], inline=False)
            if cmd.parameters and cmd.parameters[-1].type is OT.string and not cmd.parameters[-1].choices:
                embed.add_field(name="Tip", inline=False,
                                value=f"Everything after the earlier options counts as `{cmd.parameters[-1].display_name}` - no quotes needed.")
            example = EXAMPLES.get(cmd.qualified_name)
            if example:
                embed.add_field(name="Example", value=f"`{example.format(p=p)}`", inline=False)
        if cmd.qualified_name in SLASH_ONLY:
            embed.set_footer(text="This one opens a form or private panel, so it only works as /" + cmd.qualified_name)
        await message.reply(embed=embed, mention_author=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(PrefixHelp(bot))
