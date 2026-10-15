import os
import re
import time
import datetime
import discord
from discord import app_commands
from discord.ext import commands

import config
import command_perms
import database as db
import guild_config as gc
from permissions import owner_ids, is_owner as _is_owner_id, get_prefixes

# Rules are per-server and start out unset — there is no default/hardcoded rules
# text. Admins set them with /setrules, which saves to the settings table under
# the "rules_text" key (see database.get_setting / database.set_setting).
RULES_SETTING_KEY = "rules_text"


def is_owner(user) -> bool:
    return _is_owner_id(user.id)


def is_admin_perm(member: discord.Member) -> bool:
    if is_owner(member):
        return True
    if command_perms.granted():
        return True
    return member.guild_permissions.administrator


def is_staff(member: discord.Member) -> bool:
    if is_owner(member):
        return True
    if command_perms.granted():
        return True
    role_ids = {r.id for r in member.roles}
    return gc.get(member.guild.id, "staff-role") in role_ids or gc.get(member.guild.id, "admin-role") in role_ids


def parse_duration(text: str):
    """Returns seconds, or None for infinite/permanent."""
    text = (text or "").strip().lower()
    if text in ("infinite", "inf", "permanent", "perm", "forever", "0"):
        return None
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    total = 0
    num = ""
    for ch in text:
        if ch.isdigit():
            num += ch
        elif ch in units and num:
            total += int(num) * units[ch]
            num = ""
    if num:  # bare number = minutes
        total += int(num) * 60
    return total if total > 0 else None


def rules_embed(guild: discord.Guild) -> discord.Embed:
    text = db.get_setting(guild.id, RULES_SETTING_KEY)
    description = text if text else "No rules have been set for this server yet. Ask an admin to run `/setrules`."
    return discord.Embed(title=f"📜 {guild.name} — Server Rules", description=description, color=discord.Color.blurple())


class RulesLinkView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        if config.RULES_URL:
            self.add_item(discord.ui.Button(label="View Full Rules", url=config.RULES_URL))


WELCOME_MESSAGE_KEY = "welcome_message"
WELCOME_IMAGE_URL_KEY = "welcome_image_url"
MAX_WELCOME_IMAGE_BYTES = 8 * 1024 * 1024
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def _welcome_image_dir() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(db.DB_PATH)), "welcome_images")


def welcome_image_path(guild_id: int):
    """Path of an uploaded welcome image for this server, or None. Uploads are saved to disk
    (Discord's attachment links expire), so the image keeps working."""
    d = _welcome_image_dir()
    for ext in _IMAGE_EXTS:
        p = os.path.join(d, f"{guild_id}{ext}")
        if os.path.exists(p):
            return p
    return None


def clear_welcome_image(guild_id: int):
    p = welcome_image_path(guild_id)
    if p:
        try:
            os.remove(p)
        except OSError:
            pass
    db.delete_setting(guild_id, WELCOME_IMAGE_URL_KEY)


def _welcome_image(guild_id: int):
    """(discord.File | None, image_url | None). The File is new each call - Files are single-use."""
    path = welcome_image_path(guild_id)
    if path:
        name = "welcome" + os.path.splitext(path)[1]
        return discord.File(path, filename=name), f"attachment://{name}"
    url = db.get_setting(guild_id, WELCOME_IMAGE_URL_KEY)
    return None, url or None


def preview_kwargs(header: str, payload: dict) -> dict:
    """send_message kwargs showing a welcome payload under a header line (ephemeral)."""
    kw = {"ephemeral": True}
    if "embed" in payload:
        kw["content"] = header
        kw["embed"] = payload["embed"]
    else:
        kw["content"] = f"{header}\n\n{payload['content']}"[:2000]
    if payload.get("file") is not None:
        kw["file"] = payload["file"]
    return kw


def _ordinal(n: int) -> str:
    suffix = "th"
    if n % 100 not in (11, 12, 13):
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


_ROLE_MENTION_RE = re.compile(r"<@&(\d+)>")


def _resolve_role_names(text: str, guild: discord.Guild) -> str:
    """Lets admins write plain `@RoleName` in the welcome message (a modal can't turn that into
    a real mention on its own). Longest names first so "@Staff Team" wins over "@Staff"."""
    for role in sorted(guild.roles, key=lambda r: len(r.name), reverse=True):
        if role.is_default() or not role.name.strip():
            continue
        text = re.sub(r"(?<![\w<])@" + re.escape(role.name) + r"(?!\w)", role.mention, text, flags=re.IGNORECASE)
    return text


def welcome_pings(member: discord.Member, text: str):
    """Returns (role_ids, AllowedMentions) for a welcome: the joining member plus every role
    mentioned in the welcome text. @everyone/@here are never allowed."""
    guild = member.guild
    role_ids = []
    for rid in _ROLE_MENTION_RE.findall(text or ""):
        rid = int(rid)
        if rid not in role_ids and guild.get_role(rid):
            role_ids.append(rid)
    allowed = discord.AllowedMentions(
        users=[member], roles=[discord.Object(id=r) for r in role_ids], everyone=False, replied_user=False,
    )
    return role_ids, allowed


def unpingable_roles(guild: discord.Guild, text: str):
    """Roles in the welcome text that Discord won't actually notify: the role isn't set to
    'Allow anyone to @mention this role' and the bot lacks 'Mention @everyone, @here and All Roles'."""
    me = guild.me
    if me is not None and me.guild_permissions.mention_everyone:
        return []
    out = []
    for rid in _ROLE_MENTION_RE.findall(text or ""):
        role = guild.get_role(int(rid))
        if role and not role.mentionable and role not in out:
            out.append(role)
    return out


WELCOME_STYLE_KEY = "welcome_style"  # "embed" (default) or "plain"


def _welcome_description(member: discord.Member) -> str:
    """The server's custom welcome message (or the default), with placeholders and @RoleName
    filled in. Shared by the embed and plain-message styles."""
    guild = member.guild
    ordinal = _ordinal(guild.member_count or 0)

    template = db.get_setting(guild.id, WELCOME_MESSAGE_KEY)
    if template:
        return (
            _resolve_role_names(template, guild)
            .replace("{user}", member.mention)
            .replace("{server}", guild.name)
            .replace("{inviter}", "")
            .replace("{count}", ordinal)
        )
    return (
        f"{member.mention} Has Joined **{guild.name}**\n"
        f"They are our **{ordinal}** member!"
    )


def build_welcome_embed(member: discord.Member) -> discord.Embed:
    """The welcome embed: the server's custom message (or the default) on top, and the
    member's profile ALWAYS underneath it - the profile is not optional, so a custom message
    can never remove it. Shared by the join listener and the /welcome message preview."""
    guild = member.guild
    ordinal = _ordinal(guild.member_count or 0)
    description = _welcome_description(member)

    embed = discord.Embed(
        title="👋 Welcome!",
        description=description,
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow(),
    )

    # ---- Profile (always shown, always below the custom message) ----
    joined = member.joined_at or discord.utils.utcnow()
    embed.add_field(name="👤 Member", value=f"{member.mention}\n`{member}`", inline=True)
    embed.add_field(name="🆔 User ID", value=f"`{member.id}`", inline=True)
    embed.add_field(name="🔢 Member #", value=f"**{ordinal}**", inline=True)
    embed.add_field(name="📅 Account Created", value=discord.utils.format_dt(member.created_at, "R"), inline=True)
    embed.add_field(name="📥 Joined Server", value=discord.utils.format_dt(joined, "R"), inline=True)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text=guild.name, icon_url=guild.icon.url if guild.icon else None)
    return embed


def build_welcome_payload(member: discord.Member) -> dict:
    """kwargs for channel.send(...) for this server's chosen welcome style. Pings always go in
    the message content, because mentions inside an embed never notify anyone."""
    guild = member.guild
    style = db.get_setting(guild.id, WELCOME_STYLE_KEY) or "embed"

    if style == "plain":
        text = _welcome_description(member)
        if member.mention not in text:  # custom message without {user}: still ping the new member
            text = f"{member.mention} {text}"
        ordinal = _ordinal(guild.member_count or 0)
        text += (f"\n-# {member} • ID `{member.id}` • Member **{ordinal}** • "
                 f"Account created {discord.utils.format_dt(member.created_at, 'R')}")
        file, image = _welcome_image(guild.id)
        if image and file is None:
            text += f"\n{image}"  # external image link: Discord shows it under the message
        if len(text) > 2000:
            text = text[:1997] + "..."
        _roles, allowed = welcome_pings(member, text)
        payload = {"content": text, "allowed_mentions": allowed}
        if file is not None:
            payload["file"] = file
        return payload

    embed = build_welcome_embed(member)
    file, image = _welcome_image(guild.id)
    if image:
        embed.set_image(url=image)
    role_ids, allowed = welcome_pings(member, embed.description)
    content = " ".join([member.mention] + [f"<@&{r}>" for r in role_ids])
    payload = {"content": content, "embed": embed, "allowed_mentions": allowed}
    if file is not None:
        payload["file"] = file
    return payload



class WelcomeMessageModal(discord.ui.Modal, title="Set Welcome Message"):
    message = discord.ui.TextInput(
        label="Custom welcome message",
        style=discord.TextStyle.paragraph,
        max_length=1500,
        required=False,
        placeholder="Welcome {user} to {server}! {user} {server} {count} - write @RoleName to ping a role",
    )

    def __init__(self, current: str = None):
        super().__init__()
        if current:
            self.message.default = current[:1500]

    async def on_submit(self, interaction: discord.Interaction):
        text = str(self.message.value or "").strip()
        if text:
            db.set_setting(interaction.guild_id, WELCOME_MESSAGE_KEY, text)
            note = "Custom welcome message saved."
        else:
            db.delete_setting(interaction.guild_id, WELCOME_MESSAGE_KEY)
            note = "Custom message cleared - the default message will be used."

        payload = build_welcome_payload(interaction.user)
        preview_text = payload["content"] if "embed" not in payload else payload["embed"].description
        channel_id = db.get_setting(interaction.guild_id, "welcome_channel_id")
        where = (f"Posting in <#{channel_id}>." if channel_id
                 else "No welcome channel is set yet - use `/welcome channel`.")
        warn = ""
        blocked = unpingable_roles(interaction.guild, preview_text)
        if blocked:
            warn = ("\n⚠️ These roles won't actually ping: " + ", ".join(r.mention for r in blocked)
                    + ". Turn on **Allow anyone to @mention this role** for them, or give me the "
                    "**Mention @everyone, @here and All Roles** permission.")
        header = f"{note} {where} Here's a preview using your profile:{warn}"
        await interaction.response.send_message(**preview_kwargs(header, payload))


class SetRulesModal(discord.ui.Modal, title="Set Server Rules"):
    rules_text = discord.ui.TextInput(
        label="Rules",
        style=discord.TextStyle.paragraph,
        max_length=4000,
        required=False,
        placeholder="Leave blank to clear the rules for this server.",
    )

    def __init__(self, current: str | None):
        super().__init__()
        if current:
            self.rules_text.default = current

    async def on_submit(self, interaction: discord.Interaction):
        new_text = str(self.rules_text.value).strip()
        if new_text:
            db.set_setting(interaction.guild_id, RULES_SETTING_KEY, new_text)
            await interaction.response.send_message("Rules updated.", ephemeral=True)
        else:
            db.delete_setting(interaction.guild_id, RULES_SETTING_KEY)
            await interaction.response.send_message("Rules cleared — this server now has no rules set.", ephemeral=True)


class Utility(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---------------- /help ----------------

    @staticmethod
    def _can_see(member: discord.Member, description: str, name: str = "") -> bool:
        if name:
            decision = command_perms.evaluate(member, member.guild.id, name)
            if decision == command_perms.ALLOW:
                return True
            if decision == command_perms.DENY:
                return False
        if description.startswith("[Owner"):
            return is_owner(member)
        if description.startswith("[Admin"):
            return is_admin_perm(member)
        if description.startswith("[Staff"):
            return is_staff(member)
        return True

    @app_commands.command(name="help", description="Show the commands you can use")
    async def help_cmd(self, interaction: discord.Interaction):
        lines = []
        for cmd in sorted(self.bot.tree.get_commands(), key=lambda c: c.name):
            if not self._can_see(interaction.user, cmd.description or "", cmd.name):
                continue
            desc = (cmd.description or "").split("] ", 1)[-1]
            if isinstance(cmd, app_commands.Group):
                subs = " | ".join(c.name for c in cmd.commands if self._can_see(interaction.user, c.description or "", c.qualified_name))
                lines.append(f"`/{cmd.name}` {subs} — {desc}")
            else:
                lines.append(f"`/{cmd.name}` — {desc}")

        embed = discord.Embed(title="❓ Help", color=discord.Color.blurple())
        chunk, size, n = [], 0, 1
        for line in lines + [None]:
            if line is None or size + len(line) + 1 > 1000:
                embed.add_field(name="Commands" if n == 1 else "Commands (cont.)", value="\n".join(chunk), inline=False)
                chunk, size, n = [], 0, n + 1
            if line is not None:
                chunk.append(line)
                size += len(line) + 1
        prefixes = get_prefixes()
        if prefixes:
            examples = ", ".join(f"`{p} rules`" for p in prefixes[:3])
            embed.add_field(
                name="Text commands",
                value=f"You can also type commands with a prefix instead of `/`, e.g. {examples}.",
                inline=False,
            )
        embed.set_footer(text="You only see the commands you have permission to use.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---------------- /ping ----------------

    @app_commands.command(name="ping", description="Check the bot's response time")
    async def ping(self, interaction: discord.Interaction):
        start = time.perf_counter()
        await interaction.response.send_message("Pinging...")
        elapsed_ms = (time.perf_counter() - start) * 1000
        await interaction.edit_original_response(content=f"Pong! Replied within **{elapsed_ms:.0f}ms**")

    # ---------------- /rules ----------------

    @app_commands.command(name="rules", description="View the server rules")
    async def rules(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=rules_embed(interaction.guild), view=RulesLinkView(), ephemeral=True)

    # ---------------- /setrules ----------------

    @app_commands.command(name="setrules", description="[Admin] Set or clear this server's rules")
    async def setrules(self, interaction: discord.Interaction):
        if not is_admin_perm(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)

        current = db.get_setting(interaction.guild_id, RULES_SETTING_KEY)
        await interaction.response.send_modal(SetRulesModal(current))

    # ---------------- /lock ----------------

    @app_commands.command(name="lock", description="[Admin] Lock a channel so nobody can type")
    @app_commands.describe(
        time="How long to lock for (e.g. 30m, 2h, 1d, or 'infinite')",
        reason="Why the channel is being locked",
        channel="Channel to lock (defaults to this one)",
    )
    async def lock(self, interaction: discord.Interaction, time: str, reason: str,
                    channel: discord.TextChannel = None):
        if not is_admin_perm(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)

        target = channel or interaction.channel
        seconds = parse_duration(time)

        try:
            await target.set_permissions(interaction.guild.default_role, send_messages=False,
                                          reason=f"Locked by {interaction.user}: {reason}")
        except discord.Forbidden:
            return await interaction.response.send_message("I don't have permission to lock that channel.", ephemeral=True)

        duration_text = "indefinitely" if seconds is None else f"for {time}"
        embed = discord.Embed(
            title="🔒 Channel Locked",
            description=f"This channel has been locked {duration_text}.\n\n**Reason:** {reason}",
            color=discord.Color.red(),
        )
        embed.set_footer(text=f"Locked by {interaction.user}")
        await target.send(embed=embed)
        await interaction.response.send_message(f"Locked {target.mention} {duration_text}.", ephemeral=True)

        if seconds is not None:
            self.bot.loop.create_task(self._auto_unlock(target, seconds))

    async def _auto_unlock(self, channel: discord.TextChannel, seconds: int):
        import asyncio
        await asyncio.sleep(seconds)
        try:
            await channel.set_permissions(channel.guild.default_role, send_messages=None, reason="Lock expired")
            await channel.send(embed=discord.Embed(
                title="🔓 Channel Unlocked", description="The lock on this channel has expired.",
                color=discord.Color.green(),
            ))
        except discord.HTTPException:
            pass

    @app_commands.command(name="unlock", description="[Admin] Unlock a previously locked channel")
    async def unlock(self, interaction: discord.Interaction, channel: discord.TextChannel = None):
        if not is_admin_perm(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        target = channel or interaction.channel
        try:
            await target.set_permissions(interaction.guild.default_role, send_messages=None,
                                          reason=f"Unlocked by {interaction.user}")
        except discord.Forbidden:
            return await interaction.response.send_message("I don't have permission to unlock that channel.", ephemeral=True)
        await interaction.response.send_message(f"Unlocked {target.mention}.", ephemeral=True)

    # ---------------- /cooldown ----------------

    @app_commands.command(name="cooldown", description="[Admin] Set this channel's slowmode")
    @app_commands.describe(time="Slowmode delay (e.g. 10s, 5m, or 0 to disable)")
    async def cooldown(self, interaction: discord.Interaction, time: str, channel: discord.TextChannel = None):
        if not is_admin_perm(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)

        target = channel or interaction.channel
        seconds = parse_duration(time) or 0
        if seconds > 21600:
            return await interaction.response.send_message("Discord's maximum slowmode is 6 hours.", ephemeral=True)

        try:
            await target.edit(slowmode_delay=seconds, reason=f"Slowmode set by {interaction.user}")
        except discord.Forbidden:
            return await interaction.response.send_message("I don't have permission to edit that channel.", ephemeral=True)

        msg = "Slowmode disabled." if seconds == 0 else f"Slowmode set to **{seconds}s**."
        await interaction.response.send_message(f"{msg} ({target.mention})", ephemeral=True)

    # ---------------- /purge ----------------

    @app_commands.command(name="purge", description="[Staff] Bulk-delete messages in this channel")
    @app_commands.describe(number="How many messages to delete (1-100)", reason="Why you're purging")
    async def purge(self, interaction: discord.Interaction, number: app_commands.Range[int, 1, 100], reason: str):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        try:
            deleted = await interaction.channel.purge(
                limit=number,
                reason=f"purge command by {interaction.user}: Purged {number} messages with the reason {reason}",
            )
        except discord.Forbidden:
            return await interaction.followup.send("I don't have permission to delete messages here.", ephemeral=True)

        await interaction.followup.send(f"Deleted **{len(deleted)}** message(s).", ephemeral=True)

        log = self.bot.get_channel(gc.get(interaction.guild_id, "staff-action-log-channel"))
        if log:
            embed = discord.Embed(
                title="🧹 Purge",
                description=f"purge command by {interaction.user.mention}: Purged **{len(deleted)}** messages "
                             f"with the reason **{reason}**",
                color=discord.Color.orange(),
            )
            embed.add_field(name="Channel", value=interaction.channel.mention, inline=True)
            await log.send(embed=embed)

    # ---------------- /welcome ----------------

    welcome_group = app_commands.Group(name="welcome", description="[Admin] Configure the welcome message")

    @welcome_group.command(name="channel", description="[Admin] Set the channel welcome messages get posted in")
    @app_commands.describe(channel="Channel welcome messages get posted in")
    async def welcome_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not is_admin_perm(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        db.set_setting(interaction.guild_id, "welcome_channel_id", channel.id)
        await interaction.response.send_message(
            f"Welcome messages will be posted in {channel.mention}. "
            "Use `/welcome message` to customize the text.",
            ephemeral=True,
        )

    @welcome_group.command(name="style", description="[Admin] Send welcomes as an embed or as a plain message")
    @app_commands.describe(style="How welcome messages should be sent")
    @app_commands.choices(style=[
        app_commands.Choice(name="Embed", value="embed"),
        app_commands.Choice(name="Plain message", value="plain"),
    ])
    async def welcome_style(self, interaction: discord.Interaction, style: app_commands.Choice[str]):
        if not is_admin_perm(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        db.set_setting(interaction.guild_id, WELCOME_STYLE_KEY, style.value)
        channel_id = db.get_setting(interaction.guild_id, "welcome_channel_id")
        where = (f"Posting in <#{channel_id}>." if channel_id
                 else "No welcome channel is set yet - use `/welcome channel`.")
        payload = build_welcome_payload(interaction.user)
        header = f"✅ Welcomes will now be sent as **{style.name.lower()}**. {where} Preview:"
        await interaction.response.send_message(**preview_kwargs(header, payload))

    @welcome_group.command(name="message", description="[Admin] Open a popup to write the custom welcome message")
    async def welcome_message(self, interaction: discord.Interaction):
        if not is_admin_perm(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        current = db.get_setting(interaction.guild_id, WELCOME_MESSAGE_KEY)
        await interaction.response.send_modal(WelcomeMessageModal(current))

    @welcome_group.command(name="image", description="[Admin] Add an image to the welcome message (upload one, or give a link)")
    @app_commands.describe(image="Upload an image", url="...or a direct link to an image",
                           remove="Remove the current welcome image")
    async def welcome_image(self, interaction: discord.Interaction, image: discord.Attachment = None,
                            url: str = None, remove: bool = False):
        if not is_admin_perm(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        gid = interaction.guild_id

        if remove:
            clear_welcome_image(gid)
            return await interaction.response.send_message("🗑️ Welcome image removed.", ephemeral=True)

        if image is not None:
            ext = os.path.splitext(image.filename)[1].lower()
            if ext not in _IMAGE_EXTS or not (image.content_type or "").startswith("image/"):
                return await interaction.response.send_message(
                    "That doesn't look like an image (png, jpg, gif or webp).", ephemeral=True)
            if image.size > MAX_WELCOME_IMAGE_BYTES:
                return await interaction.response.send_message("That image is over 8 MB.", ephemeral=True)
            data = await image.read()
            clear_welcome_image(gid)
            os.makedirs(_welcome_image_dir(), exist_ok=True)
            with open(os.path.join(_welcome_image_dir(), f"{gid}{ext}"), "wb") as f:
                f.write(data)
            note = "✅ Welcome image saved."
        elif url:
            url = url.strip()
            if not url.lower().startswith(("http://", "https://")):
                return await interaction.response.send_message("The link must start with http:// or https://", ephemeral=True)
            clear_welcome_image(gid)
            db.set_setting(gid, WELCOME_IMAGE_URL_KEY, url)
            note = "✅ Welcome image link saved."
        else:
            return await interaction.response.send_message(
                "Upload an image, give a link, or set `remove` to true.", ephemeral=True)

        channel_id = db.get_setting(gid, "welcome_channel_id")
        where = (f"Posting in <#{channel_id}>." if channel_id
                 else "No welcome channel is set yet - use `/welcome channel`.")
        await interaction.response.send_message(
            **preview_kwargs(f"{note} {where} Preview:", build_welcome_payload(interaction.user)))

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        channel_id = db.get_setting(member.guild.id, "welcome_channel_id")
        if not channel_id:
            return
        channel = member.guild.get_channel(int(channel_id))
        if not channel:
            return

        try:
            await channel.send(**build_welcome_payload(member))
        except discord.HTTPException:
            pass

    # ---------------- /bot-info ----------------

    @app_commands.command(name="bot-info", description="[Admin] View information about the bot")
    async def botinfo_cmd(self, interaction: discord.Interaction):
        if not is_admin_perm(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)

        import sys
        app_info = await self.bot.application_info()
        uptime = "Unknown"
        if hasattr(self.bot, "start_time"):
            delta = datetime.datetime.now(datetime.timezone.utc) - self.bot.start_time
            hours, rem = divmod(int(delta.total_seconds()), 3600)
            minutes, seconds = divmod(rem, 60)
            uptime = f"{hours}h {minutes}m {seconds}s"

        embed = discord.Embed(title=f"🤖 {self.bot.user.name}", color=discord.Color.blurple())
        embed.set_thumbnail(url=self.bot.user.display_avatar.url)
        embed.add_field(name="Bot ID", value=str(self.bot.user.id), inline=True)
        embed.add_field(name="Owners", value=" ".join(f"<@{uid}>" for uid in sorted(owner_ids())), inline=True)
        embed.add_field(name="Uptime", value=uptime, inline=True)
        embed.add_field(name="Latency", value=f"{self.bot.latency * 1000:.0f}ms", inline=True)
        embed.add_field(name="discord.py", value=discord.__version__, inline=True)
        embed.add_field(name="Python", value=sys.version.split()[0], inline=True)
        embed.add_field(name="Cogs Loaded", value=str(len(self.bot.cogs)), inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Utility(bot))
