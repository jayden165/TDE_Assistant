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
import welcome_card
from permissions import owner_ids, is_owner as _is_owner_id, get_prefixes

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


WELCOME_MESSAGE_KEY = "welcome_message"
WELCOME_IMAGE_URL_KEY = "welcome_image_url"
WELCOME_DETAILS_KEY = "welcome_details"  # member info line / profile fields; on unless set to "off"
WELCOME_CARD_KEY = "welcome_card"  # per-member "Member #N" card; on unless set to "off"
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


def welcome_details_enabled(guild_id: int) -> bool:
    return (db.get_setting(guild_id, WELCOME_DETAILS_KEY) or "on") != "off"


def welcome_card_enabled(guild_id: int) -> bool:
    return (db.get_setting(guild_id, WELCOME_CARD_KEY) or "on") != "off"


async def _welcome_media(member: discord.Member):
    """(discord.File | None, image_url | None) for this member's welcome: a card made fresh for
    them (name, avatar, member #), or - if cards are off or one can't be made - the server's
    normal welcome image."""
    if welcome_card_enabled(member.guild.id):
        file = await welcome_card.build_card_file(member)
        if file is not None:
            return file, f"attachment://{welcome_card.CARD_FILENAME}"
    return _welcome_image(member.guild.id)


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
    can never remove it. Shared by the join listener and the /welcome preview."""
    guild = member.guild
    ordinal = _ordinal(guild.member_count or 0)
    description = _welcome_description(member)

    embed = discord.Embed(
        title="👋 Welcome!",
        description=description,
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow(),
    )

    # ---- Profile (below the custom message; admins can hide it with /welcome details:False) ----
    if welcome_details_enabled(guild.id):
        joined = member.joined_at or discord.utils.utcnow()
        embed.add_field(name="👤 Member", value=f"{member.mention}\n`{member}`", inline=True)
        embed.add_field(name="🆔 User ID", value=f"`{member.id}`", inline=True)
        embed.add_field(name="🔢 Member #", value=f"**{ordinal}**", inline=True)
        embed.add_field(name="📅 Account Created", value=discord.utils.format_dt(member.created_at, "R"), inline=True)
        embed.add_field(name="📥 Joined Server", value=discord.utils.format_dt(joined, "R"), inline=True)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text=guild.name, icon_url=guild.icon.url if guild.icon else None)
    return embed


async def build_welcome_payload(member: discord.Member) -> dict:
    """kwargs for channel.send(...) for this server's chosen welcome style. Pings always go in
    the message content, because mentions inside an embed never notify anyone."""
    guild = member.guild
    style = db.get_setting(guild.id, WELCOME_STYLE_KEY) or "embed"

    if style == "plain":
        text = _welcome_description(member)
        if member.mention not in text:  # custom message without {user}: still ping the new member
            text = f"{member.mention} {text}"
        if welcome_details_enabled(guild.id):
            ordinal = _ordinal(guild.member_count or 0)
            text += (f"\n-# {member} • ID `{member.id}` • Member **{ordinal}** • "
                     f"Account created {discord.utils.format_dt(member.created_at, 'R')}")
        file, image = await _welcome_media(member)
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
    file, image = await _welcome_media(member)
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
        await interaction.response.defer(ephemeral=True)  # making the welcome card takes a moment
        text = str(self.message.value or "").strip()
        if text:
            db.set_setting(interaction.guild_id, WELCOME_MESSAGE_KEY, text)
            note = "Custom welcome message saved."
        else:
            db.delete_setting(interaction.guild_id, WELCOME_MESSAGE_KEY)
            note = "Custom message cleared - the default message will be used."

        payload = await build_welcome_payload(interaction.user)
        preview_text = payload["content"] if "embed" not in payload else payload["embed"].description
        channel_id = db.get_setting(interaction.guild_id, "welcome_channel_id")
        where = (f"Posting in <#{channel_id}>." if channel_id
                 else "No welcome channel is set yet - use `/welcome channel:#...`.")
        warn = ""
        blocked = unpingable_roles(interaction.guild, preview_text)
        if blocked:
            warn = ("\n⚠️ These roles won't actually ping: " + ", ".join(r.mention for r in blocked)
                    + ". Turn on **Allow anyone to @mention this role** for them, or give me the "
                    "**Mention @everyone, @here and All Roles** permission.")
        header = f"{note} {where} Here's a preview using your profile:{warn}"
        await interaction.followup.send(**preview_kwargs(header, payload))


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
        if description.startswith("[Mod"):
            perms = member.guild_permissions
            return (is_owner(member) or command_perms.granted() or perms.administrator
                    or perms.ban_members or perms.kick_members or perms.moderate_members)
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
            examples = ", ".join(f"`{p} ping`" for p in prefixes[:3])
            embed.add_field(
                name="Text commands",
                value=f"You can also type commands with a prefix instead of `/`, e.g. {examples}. "
                      f"Type `{prefixes[0]} help` to see them all.",
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

    # ---------------- /channel (lock, slowmode, purge) ----------------

    channel_group = app_commands.Group(name="channel", description="[Staff] Lock (or unlock), slow down or clean up a channel")

    @channel_group.command(name="lock", description="[Admin] Lock a channel so nobody can type - or use 'off' as the time to unlock it")
    @app_commands.describe(
        time="How long to lock for (e.g. 30m, 2h, 1d, or 'infinite') - or 'off' to unlock the channel",
        reason="Why the channel is being locked (needed to lock)",
        channel="Channel to lock or unlock (defaults to this one)",
    )
    async def lock(self, interaction: discord.Interaction, time: str, reason: str = None,
                    channel: discord.TextChannel = None):
        if not is_admin_perm(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)

        target = channel or interaction.channel
        if time.strip().lower() in ("off", "unlock"):  # 'off' lifts a lock
            try:
                await target.set_permissions(interaction.guild.default_role, send_messages=None,
                                              reason=f"Unlocked by {interaction.user}")
            except discord.Forbidden:
                return await interaction.response.send_message("I don't have permission to unlock that channel.", ephemeral=True)
            return await interaction.response.send_message(f"Unlocked {target.mention}.", ephemeral=True)
        if not reason:
            return await interaction.response.send_message("Give a reason to lock the channel.", ephemeral=True)
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

    @channel_group.command(name="slowmode", description="[Admin] Set a channel's slowmode (0 turns it off)")
    @app_commands.describe(time="Slowmode delay (e.g. 10s, 5m, or 0 to disable)")
    async def slowmode(self, interaction: discord.Interaction, time: str, channel: discord.TextChannel = None):
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

    @channel_group.command(name="purge", description="[Staff] Bulk-delete messages in this channel")
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

    @app_commands.command(name="welcome", description="[Admin] Set up the welcome message - run it with no options to preview it")
    @app_commands.describe(channel="Channel welcome messages get posted in",
                           style="How welcome messages should be sent",
                           image="Upload an image for the welcome message",
                           url="...or a direct link to an image",
                           remove_image="Remove the current welcome image",
                           details="Show the member info line (name, ID, member #, account age) - default: on",
                           card="Turn the per-member 'Member #N' welcome card on or off (default: on)",
                           edit_message="Open a popup to write the custom welcome message")
    @app_commands.choices(style=[
        app_commands.Choice(name="Embed", value="embed"),
        app_commands.Choice(name="Plain message", value="plain"),
    ])
    async def welcome(self, interaction: discord.Interaction, channel: discord.TextChannel = None,
                      style: app_commands.Choice[str] = None, image: discord.Attachment = None,
                      url: str = None, remove_image: bool = False, details: bool = None,
                      card: bool = None,
                      edit_message: bool = False):
        if not is_admin_perm(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        gid = interaction.guild_id

        # check the image first, so a bad one doesn't leave the other settings half-applied
        if image is not None and not remove_image:
            ext = os.path.splitext(image.filename)[1].lower()
            if ext not in _IMAGE_EXTS or not (image.content_type or "").startswith("image/"):
                return await interaction.response.send_message(
                    "That doesn't look like an image (png, jpg, gif or webp).", ephemeral=True)
            if image.size > MAX_WELCOME_IMAGE_BYTES:
                return await interaction.response.send_message("That image is over 8 MB.", ephemeral=True)
        elif url and not remove_image and not url.strip().lower().startswith(("http://", "https://")):
            return await interaction.response.send_message("The link must start with http:// or https://", ephemeral=True)

        notes = []
        if channel is not None:
            db.set_setting(gid, "welcome_channel_id", channel.id)
            notes.append(f"✅ Welcomes will be posted in {channel.mention}.")
        if style is not None:
            db.set_setting(gid, WELCOME_STYLE_KEY, style.value)
            notes.append(f"✅ Welcomes will be sent as **{style.name.lower()}**.")
        if details is not None:
            db.set_setting(gid, WELCOME_DETAILS_KEY, "on" if details else "off")
            notes.append("✅ Member info line turned **on**." if details else "✅ Member info line turned **off**.")
        if card is not None:
            db.set_setting(gid, WELCOME_CARD_KEY, "on" if card else "off")
            notes.append("✅ Welcome card turned **on** - every new member gets their own card." if card
                         else "✅ Welcome card turned **off** - the server's welcome image (if any) is used instead.")
        if remove_image:
            clear_welcome_image(gid)
            notes.append("🗑️ Welcome image removed.")
        elif image is not None:
            ext = os.path.splitext(image.filename)[1].lower()
            data = await image.read()
            clear_welcome_image(gid)
            os.makedirs(_welcome_image_dir(), exist_ok=True)
            with open(os.path.join(_welcome_image_dir(), f"{gid}{ext}"), "wb") as f:
                f.write(data)
            notes.append("✅ Welcome image saved.")
        elif url:
            clear_welcome_image(gid)
            db.set_setting(gid, WELCOME_IMAGE_URL_KEY, url.strip())
            notes.append("✅ Welcome image link saved.")

        if edit_message:  # the popup has to be the one reply to this command
            current = db.get_setting(gid, WELCOME_MESSAGE_KEY)
            return await interaction.response.send_modal(WelcomeMessageModal(current))

        channel_id = db.get_setting(gid, "welcome_channel_id")
        where = (f"Posting in <#{channel_id}>." if channel_id
                 else "No welcome channel is set yet - use `/welcome channel:#...`.")
        header = " ".join(notes + [where, "Preview:"]) if notes else f"Current welcome setup. {where} Preview:"
        await interaction.response.defer(ephemeral=True)  # making the welcome card takes a moment
        await interaction.followup.send(**preview_kwargs(header, await build_welcome_payload(interaction.user)))

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        channel_id = db.get_setting(member.guild.id, "welcome_channel_id")
        if not channel_id:
            return
        channel = member.guild.get_channel(int(channel_id))
        if not channel:
            return

        try:
            await channel.send(**(await build_welcome_payload(member)))
        except discord.HTTPException:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(Utility(bot))
