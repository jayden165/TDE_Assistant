"""
Modmail + the tickets/modmail switch.

/support-mode  (admins)  picks how members get support:  🎫 Tickets · 📬 Modmail · both
                         (stored by support.py; switching only affects NEW conversations)

How modmail works
  * A member DMs the bot -> the bot asks "contact staff?" -> a private channel is opened in the modmail
    category (falls back to the ticket category), visible to the admin role only, with an info card (account age,
    join date, roles, past threads, infractions) and Claim / Unclaim / Anonymous / Close buttons.
  * Everything staff type in that channel is sent to the member as an embed; everything the member DMs is
    shown to staff. Attachments are copied across. Edits on either side are synced. ✅ means delivered.
  * Start a message with // to leave an internal note staff-only (not sent). Messages that start with a
    bot prefix + a real command are treated as commands, not replies.
  * Replies can be anonymous (the member sees "Staff Team" instead of your name).
  * Closing (button, /modmail close, or the member's own Close button) saves a transcript to the modmail
    log channel (falls back to the transcript channel), DMs the member and removes the channel. Idle
    threads auto-close after 7 days (change with /support-mode autoclose_days, 0 = never).
  * /modmail contact starts a thread with a member, block (run again to unblock; no member = the
    blocklist) stops abuse, history shows a member's past threads. Deleting a thread channel by hand closes the thread properly.
  * Open threads keep working whatever the mode is; the mode only decides whether NEW ones can start.
"""
import asyncio
import io
import json
import logging
import time
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

import command_perms
import config
import database as db
import guild_config as gc
import permissions
import support
from cogs.custom_commands import get_custom
from cogs.tickets import is_admin, slugify

AUTOCLOSE_KEY = "modmail_autoclose_days"
DEFAULT_AUTOCLOSE_DAYS = 7
MAX_UPLOAD_BYTES = 8_000_000
NOTE_PREFIX = "//"
MIN_DETAIL = 30   # characters of real detail required before a modmail is sent to staff
BLURPLE, GREEN, RED, GREY = (discord.Color.blurple(), discord.Color.green(),
                             discord.Color.red(), discord.Color.dark_grey())


# ---------------- small helpers ----------------

def attachment_refs(message: discord.Message) -> list[str]:
    return [f"{a.filename}|{a.url}" for a in message.attachments]


async def copy_attachments(attachments: list) -> tuple[list, list]:
    """Re-uploads attachments so they survive the source message being deleted. Anything too big (or
    that fails) comes back as a link instead. Returns (files, link_lines)."""
    files, links, total = [], [], 0
    for a in attachments:
        if len(files) < 10 and total + a.size <= MAX_UPLOAD_BYTES:
            try:
                files.append(await a.to_file())
                total += a.size
                continue
            except discord.HTTPException:
                pass
        links.append(f"[{a.filename}]({a.url})")
    return files, links


def error(text: str) -> discord.Embed:
    return discord.Embed(title="❌ Couldn't do that", description=text, color=RED)


def build_transcript(thread, user_label: str) -> str:
    lines = [f"Modmail thread #{thread['id']} with {user_label}",
             f"Opened: {datetime.fromtimestamp(thread['created_at'], timezone.utc):%Y-%m-%d %H:%M:%S} UTC", ""]
    tags = {"user": "USER", "staff": "STAFF", "note": "NOTE", "system": "SYSTEM"}
    for m in db.mm_messages(thread["id"]):
        stamp = datetime.fromtimestamp(m["created_at"], timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        text = m["content"] or ""
        try:
            files = [a.split("|", 1)[1] for a in json.loads(m["attachments"] or "[]") if "|" in a]
        except (ValueError, TypeError):
            files = []
        if files:
            text += " " + " ".join(files)
        lines.append(f"[{stamp}] [{tags.get(m['source'], m['source'])}] {m['author_name']} ({m['author_id']}): {text}".rstrip())
    return "\n".join(lines)


# ---------------- views ----------------

class ThreadControlView(discord.ui.View):
    """Buttons on the info card inside a modmail channel. Persistent; the thread is found by channel."""

    def __init__(self):
        super().__init__(timeout=None)

    @staticmethod
    def _cog(interaction) -> "Modmail":
        return interaction.client.get_cog("Modmail")

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.primary, custom_id="mm_claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._cog(interaction).claim(interaction)

    @discord.ui.button(label="Unclaim", style=discord.ButtonStyle.secondary, custom_id="mm_unclaim")
    async def unclaim(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._cog(interaction).unclaim(interaction)

    @discord.ui.button(label="Anonymous", style=discord.ButtonStyle.secondary, emoji="🕶️", custom_id="mm_anon")
    async def anonymous(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._cog(interaction).toggle_anonymous(interaction)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, custom_id="mm_close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        await interaction.response.send_modal(CloseModal(self._cog(interaction)))


class CloseModal(discord.ui.Modal, title="Close this thread"):
    reason = discord.ui.TextInput(label="Reason (optional, shown to the member)", style=discord.TextStyle.paragraph,
                                  max_length=500, required=False)

    def __init__(self, cog: "Modmail"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.close_from_interaction(interaction, str(self.reason.value).strip())


class UserCloseView(discord.ui.View):
    """The Close button on the DM the member gets when their thread opens. Persistent."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close thread", style=discord.ButtonStyle.secondary, emoji="🔒", custom_id="mm_user_close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog: "Modmail" = interaction.client.get_cog("Modmail")
        guild = cog.resolve_guild()
        thread = db.mm_get_open_by_user(guild.id, interaction.user.id) if guild else None
        if thread is None:
            return await interaction.response.edit_message(view=None)
        await interaction.response.edit_message(view=None)
        await cog.close_thread(thread, guild, closer=interaction.user, reason="Closed by the member.", by_user=True)


class OpenConfirmView(discord.ui.View):
    """'Contact staff?' prompt shown to a member's first DM, so a stray message doesn't open a thread."""

    def __init__(self, cog: "Modmail", guild: discord.Guild, member: discord.Member, message: discord.Message):
        super().__init__(timeout=120)
        self.cog, self.guild, self.member, self.message = cog, guild, member, message
        self.prompt: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.member.id

    async def on_timeout(self):
        self.cog.opening.discard(self.member.id)
        if self.prompt:
            try:
                await self.prompt.edit(embed=discord.Embed(description="That timed out - send me a message again whenever you're ready.", color=GREY), view=None)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Yes, contact staff", style=discord.ButtonStyle.success)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(embed=discord.Embed(description="Opening a thread...", color=BLURPLE), view=None)
        try:
            opened = await self.cog.open_thread(self.guild, self.member)
            if opened is None:
                return await interaction.edit_original_response(embed=error(
                    "I couldn't open a thread right now. Please try again in a bit, or ask staff another way."))
            thread, channel, _dm_ok = opened
            await self.cog.relay_to_staff(thread, channel, self.message)
            await interaction.edit_original_response(embed=discord.Embed(description="✅ Thread opened.", color=GREEN))
        finally:
            self.cog.opening.discard(self.member.id)

    @discord.ui.button(label="No", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        self.cog.opening.discard(self.member.id)
        await interaction.response.edit_message(embed=discord.Embed(description="Okay, I won't send that to staff.", color=GREY), view=None)


class PanelModal(discord.ui.Modal):
    """Form shown when a member picks the modmail option (default name: Video Suggestion) in the panel
    dropdown. The details box has a minimum length, so staff never get a one-word message."""

    def __init__(self, cog: "Modmail", label: str):
        super().__init__(title=label[:45])
        self.cog, self.label = cog, label
        self.subject = discord.ui.TextInput(label="Subject / title", max_length=100,
                                            placeholder="What is this about? e.g. the title of your video idea")
        self.details = discord.ui.TextInput(
            label="Details - tell us everything", style=discord.TextStyle.paragraph, min_length=MIN_DETAIL,
            max_length=1200, placeholder="The more detail the better - what, why, how it should work...")
        self.links = discord.ui.TextInput(label="Links or extra info (optional)", style=discord.TextStyle.paragraph,
                                          max_length=500, required=False)
        for item in (self.subject, self.details, self.links):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        subject, details, links = (str(self.subject.value).strip(), str(self.details.value).strip(),
                                   str(self.links.value).strip())
        if len(details) < MIN_DETAIL:
            return await interaction.response.send_message(embed=error(
                f"Please give a bit more detail - at least {MIN_DETAIL} characters - so staff can act on it. "
                "Pick the option again to retry."), ephemeral=True)
        text = f"**{subject}**\n\n{details}" + (f"\n\n**Links / extra info:**\n{links}" if links else "")
        await self.cog.finish_panel_submission(interaction, self.label, text)


# ---------------- the cog ----------------

class Modmail(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.opening: set[int] = set()          # members with a pending prompt / thread being created
        self.notified: dict[int, float] = {}    # last time we told a blocked/outsider member off (anti-spam)
        bot.add_view(ThreadControlView())
        bot.add_view(UserCloseView())

    async def cog_load(self):
        self.autoclose.start()

    async def cog_unload(self):
        self.autoclose.cancel()

    # ---------------- plumbing ----------------

    def resolve_guild(self) -> discord.Guild | None:
        if config.GUILD_ID:
            guild = self.bot.get_guild(config.GUILD_ID)
            if guild:
                return guild
        return self.bot.guilds[0] if len(self.bot.guilds) == 1 else None

    async def fetch_user(self, user_id: int) -> discord.User | None:
        user = self.bot.get_user(user_id)
        if user:
            return user
        try:
            return await self.bot.fetch_user(user_id)
        except discord.HTTPException:
            return None

    def _is_command_text(self, message: discord.Message) -> bool:
        """True for '<prefix><real command> ...' so staff can run commands inside a thread."""
        for prefix in permissions.get_prefixes():
            if message.content.lower().startswith(prefix.lower()):
                rest = message.content[len(prefix):].strip()
                if not rest:
                    continue
                first = rest.split()[0].lower()
                if (first in ("help", "commands", "insmp") or self.bot.tree.get_command(first)
                        or get_custom(message.guild.id, first)):
                    return True
        return False

    def _staff_overwrites(self, guild: discord.Guild) -> dict:
        ow = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True,
                                                  attach_files=True, read_message_history=True, add_reactions=True),
        }
        # Video suggestions are admin-only: the staff role is deliberately NOT given access.
        role = guild.get_role(gc.get(guild.id, "admin-role"))
        if role:
            ow[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True,
                                                   read_message_history=True)
        return ow

    # ---------------- DMs from members ----------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if message.guild is None:
            await self.handle_dm(message)
        else:
            await self.handle_staff_message(message)

    async def handle_dm(self, message: discord.Message):
        user = message.author
        apps = self.bot.get_cog("Applications")
        if apps is not None and user.id in apps.active:
            return  # mid-application: those DMs are answers, not modmail
        guild = self.resolve_guild()
        if guild is None:
            return

        thread = db.mm_get_open_by_user(guild.id, user.id)
        if thread is not None:
            channel = guild.get_channel(thread["channel_id"])
            if channel is not None:
                return await self.relay_to_staff(thread, channel, message)
            await self.close_thread(thread, guild, closer=None, reason="The thread channel was deleted.",
                                    notify_user=False, delete_channel=False)

        if not support.allows_modmail(support.get_mode(guild.id)):
            return  # tickets-only: DMs to the bot are ignored, like before
        member = guild.get_member(user.id)
        if member is None:
            return await self._rate_limited_notice(user, error(f"You need to be in **{guild.name}** to contact its staff."))
        block = db.mm_get_block(guild.id, user.id)
        if block is not None:
            return await self._rate_limited_notice(user, error("You can't use modmail on this server."))
        if user.id in self.opening:
            return
        if len((message.content or "").strip()) < MIN_DETAIL:
            # Too vague ("hi", "help") - ask for the full story first so staff get something they can act on.
            return await message.reply(embed=discord.Embed(
                title="📝 Tell me a bit more",
                description="Before I pass this to staff, I need some detail. Please send **one message** explaining "
                            "what you need - what it's about, what happened, and any links or names that help "
                            f"(at least {MIN_DETAIL} characters).",
                color=discord.Color.orange()), mention_author=False)
        self.opening.add(user.id)
        embed = discord.Embed(
            title=f"📬 Contact {guild.name} staff?",
            description="Do you want to send this message to the staff team? A private conversation will be opened "
                        "and they'll reply here. Make sure your message has all the detail they need.",
            color=BLURPLE)
        view = OpenConfirmView(self, guild, member, message)
        try:
            view.prompt = await message.reply(embed=embed, view=view, mention_author=False)
        except discord.HTTPException:
            self.opening.discard(user.id)

    async def _rate_limited_notice(self, user: discord.User, embed: discord.Embed):
        if time.time() - self.notified.get(user.id, 0) < 60:
            return
        self.notified[user.id] = time.time()
        try:
            await user.send(embed=embed)
        except discord.HTTPException:
            pass

    # ---------------- opening a thread ----------------

    async def open_thread(self, guild: discord.Guild, member: discord.Member, opened_by: discord.abc.User | None = None,
                          source: str | None = None, quote: str | None = None):
        """Creates the private channel + DB row + info card. Returns (thread_row, channel, dm_ok) or None.
        `source` is shown on the info card (e.g. the panel option used); `quote` is echoed back to the member."""
        category_id = gc.get(guild.id, "modmail-category") or gc.get(guild.id, "ticket-category")
        category = guild.get_channel(category_id) if category_id else None
        if not isinstance(category, discord.CategoryChannel):
            category = None
        try:
            channel = await guild.create_text_channel(
                name=f"modmail-{slugify(member.name) or member.id}"[:100], category=category,
                overwrites=self._staff_overwrites(guild),
                topic=f"Modmail | {member} ({member.id})", reason=f"Modmail thread for {member}")
        except discord.HTTPException:
            logging.exception("couldn't create a modmail channel")
            return None
        thread_id = db.mm_create_thread(guild.id, member.id, channel.id, opened_by.id if opened_by else None)
        thread = db.mm_get_thread(thread_id)

        # info card for staff
        embed = discord.Embed(title=f"📬 Modmail #{thread_id}", color=BLURPLE, timestamp=discord.utils.utcnow())
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Member", value=f"{member.mention}\n`{member}` ({member.id})", inline=True)
        embed.add_field(name="Account created", value=discord.utils.format_dt(member.created_at, "R"), inline=True)
        embed.add_field(name="Joined server", value=discord.utils.format_dt(member.joined_at, "R") if member.joined_at else "?", inline=True)
        roles = [r.mention for r in reversed(member.roles) if not r.is_default()][:8]
        embed.add_field(name="Roles", value=" ".join(roles) or "None", inline=False)
        past = db.mm_count_threads(guild.id, member.id) - 1
        warnings = db.count_infractions(guild.id, member.id)
        embed.add_field(name="History", value=f"{past} earlier thread(s) · {warnings} infraction(s)", inline=False)
        if source:
            embed.add_field(name="Opened from", value=f"The support panel - **{source}**", inline=False)
        if opened_by:
            embed.set_footer(text=f"Started by {opened_by}")
        embed.add_field(name="How to reply", inline=False,
                        value=f"Just type here - it's sent to the member. Start with `{NOTE_PREFIX}` for a private staff note. "
                              "Use the buttons below or `/modmail` commands.")
        admin_role = guild.get_role(gc.get(guild.id, "admin-role"))
        await channel.send(content=admin_role.mention if admin_role else None, embed=embed, view=ThreadControlView(),
                           allowed_mentions=discord.AllowedMentions(roles=True))

        # tell the member (the Close button lets them end it themselves)
        dm_ok = True
        try:
            text = "Staff will reply here as soon as they can. Anything you send me now goes to them."
            if quote:
                text += "\n\n**You sent:**\n" + "\n".join(f"> {line}" for line in quote[:1500].splitlines())
            dm = discord.Embed(
                title="📬 Conversation opened" if not opened_by else f"📬 {guild.name} staff started a conversation",
                description=text, color=GREEN)
            dm.set_footer(text=guild.name)
            await member.send(embed=dm, view=UserCloseView())
        except discord.HTTPException:
            dm_ok = False
        return thread, channel, dm_ok

    # ---------------- relaying ----------------

    async def relay_to_staff(self, thread, channel: discord.TextChannel, message: discord.Message):
        files, links = await copy_attachments(message.attachments)
        embed = discord.Embed(description=message.content or ("" if files or links else "*(no text)*"),
                              color=BLURPLE, timestamp=message.created_at)
        embed.set_author(name=str(message.author), icon_url=message.author.display_avatar.url)
        if links:
            embed.add_field(name="Attachments", value="\n".join(links)[:1024], inline=False)
        try:
            sent = await channel.send(embed=embed, files=files)
        except discord.HTTPException:
            logging.exception("couldn't relay a member message to modmail channel %s", channel.id)
            return await message.add_reaction("❌")
        db.mm_add_message(thread["id"], message.author.id, str(message.author), "user", message.content,
                          attachment_refs(message), channel_msg_id=sent.id, dm_msg_id=message.id)
        try:
            await message.add_reaction("✅")
        except discord.HTTPException:
            pass

    async def relay_text_to_staff(self, thread, channel: discord.TextChannel, user: discord.abc.User, text: str):
        """Shows a member's text in the thread when there's no DM message to copy (the panel form)."""
        embed = discord.Embed(description=text[:4000], color=BLURPLE, timestamp=discord.utils.utcnow())
        embed.set_author(name=str(user), icon_url=user.display_avatar.url)
        sent = await channel.send(embed=embed)
        db.mm_add_message(thread["id"], user.id, str(user), "user", text, channel_msg_id=sent.id)

    # ---------------- the support panel dropdown ----------------

    async def start_from_panel(self, interaction: discord.Interaction):
        """The member picked the modmail entry (default name: Video Suggestion) in the ticket panel."""
        guild, user = interaction.guild, interaction.user
        label = support.get_modmail_label(guild.id)
        if not support.allows_modmail(support.get_mode(guild.id)):
            return await interaction.response.send_message(
                embed=error(f"**{label}** isn't available right now - pick another option."), ephemeral=True)
        if db.mm_get_block(guild.id, user.id) is not None:
            return await interaction.response.send_message(embed=error("You can't use this option on this server."), ephemeral=True)
        if db.mm_get_open_by_user(guild.id, user.id) is not None:
            return await interaction.response.send_message(
                embed=discord.Embed(description="You already have an open conversation with staff - just DM me "
                                                "and your message goes straight to them.", color=BLURPLE), ephemeral=True)
        await interaction.response.send_modal(PanelModal(self, label))

    async def finish_panel_submission(self, interaction: discord.Interaction, label: str, text: str):
        guild, member = interaction.guild, interaction.user
        if member.id in self.opening or db.mm_get_open_by_user(guild.id, member.id) is not None:
            return await interaction.response.send_message(
                embed=error("You already have a conversation open with staff."), ephemeral=True)
        self.opening.add(member.id)
        await interaction.response.defer(ephemeral=True)
        try:
            opened = await self.open_thread(guild, member, source=label, quote=text)
            if opened is None:
                return await interaction.followup.send(embed=error(
                    "I couldn't open a conversation right now. Please try again in a bit, or ask staff another way."), ephemeral=True)
            thread, channel, dm_ok = opened
            await self.relay_text_to_staff(thread, channel, member, text)
        finally:
            self.opening.discard(member.id)
        note = ("Staff will reply to you in your DMs with me." if dm_ok else
                "⚠️ I couldn't DM you. Turn on **Direct Messages** for this server (Server Settings → Privacy) "
                "so staff can reply to you.")
        await interaction.followup.send(embed=discord.Embed(
            title=f"✅ {label} sent", description=note, color=GREEN if dm_ok else discord.Color.orange()), ephemeral=True)

    async def relay_to_user(self, thread, guild: discord.Guild, message: discord.Message):
        user = await self.fetch_user(thread["user_id"])
        files, links = await copy_attachments(message.attachments)
        embed = discord.Embed(description=message.content or ("" if files or links else "*(no text)*"),
                              color=GREEN, timestamp=discord.utils.utcnow())
        if thread["anonymous"]:
            embed.set_author(name="Staff Team", icon_url=guild.icon.url if guild.icon else None)
        else:
            embed.set_author(name=message.author.display_name, icon_url=message.author.display_avatar.url)
        embed.set_footer(text=guild.name)
        if links:
            embed.add_field(name="Attachments", value="\n".join(links)[:1024], inline=False)
        sent = None
        if user is not None:
            try:
                sent = await user.send(embed=embed, files=files)
            except discord.HTTPException:
                sent = None
        if sent is None:
            try:
                await message.add_reaction("❌")
            except discord.HTTPException:
                pass
            return await message.channel.send(embed=error(
                "I couldn't DM them - they may have DMs closed, or they left every server I share with them."),
                delete_after=20)
        db.mm_add_message(thread["id"], message.author.id, str(message.author), "staff", message.content,
                          attachment_refs(message), channel_msg_id=message.id, dm_msg_id=sent.id)
        try:
            await message.add_reaction("✅")
        except discord.HTTPException:
            pass

    async def handle_staff_message(self, message: discord.Message):
        thread = db.mm_get_open_by_channel(message.channel.id)
        if thread is None or not isinstance(message.author, discord.Member) or not is_admin(message.author):
            return
        if self._is_command_text(message):
            return
        content = message.content.strip()
        if content.startswith(NOTE_PREFIX):
            note = content[len(NOTE_PREFIX):].strip()
            db.mm_add_message(thread["id"], message.author.id, str(message.author), "note", note,
                              attachment_refs(message), channel_msg_id=message.id)
            try:
                await message.add_reaction("📝")
            except discord.HTTPException:
                pass
            return
        if not content and not message.attachments:
            return
        await self.relay_to_user(thread, message.guild, message)

    # ---------------- edits ----------------

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent):
        content = payload.data.get("content")
        if content is None or payload.data.get("author", {}).get("bot"):
            return
        try:
            if payload.guild_id is None:  # the member edited a DM -> update what staff see
                row = db.mm_find_user_message(payload.message_id)
                if row is None:
                    return
                thread = db.mm_get_thread(row["thread_id"])
                channel = self.bot.get_channel(thread["channel_id"]) if thread and thread["status"] == "open" else None
                if channel is None:
                    return
                relayed = await channel.fetch_message(row["channel_msg_id"])
                await self._edit_embed(relayed, content)
            else:  # staff edited a reply -> update what the member sees
                row = db.mm_find_staff_message(payload.message_id)
                if row is None:
                    return
                thread = db.mm_get_thread(row["thread_id"])
                if thread is None or thread["status"] != "open":
                    return
                user = await self.fetch_user(thread["user_id"])
                dm = user.dm_channel or await user.create_dm()
                relayed = await dm.fetch_message(row["dm_msg_id"])
                await self._edit_embed(relayed, content)
            db.mm_update_message_content(row["id"], content)
        except (discord.HTTPException, AttributeError):
            pass

    @staticmethod
    async def _edit_embed(message: discord.Message, content: str):
        if not message.embeds:
            return
        embed = message.embeds[0]
        embed.description = content or "*(no text)*"
        embed.set_footer(text=((embed.footer.text + " · ") if embed.footer and embed.footer.text else "") + "edited")
        await message.edit(embed=embed)

    # ---------------- closing ----------------

    async def close_thread(self, thread, guild: discord.Guild, closer, reason: str | None, notify_user: bool = True,
                           delete_channel: bool = True, by_user: bool = False):
        """Marks it closed, saves the transcript to the log channel, tells the member, removes the channel."""
        reason = reason or "No reason given"
        closer_label = str(closer) if closer else "the bot"
        db.mm_add_message(thread["id"], closer.id if closer else self.bot.user.id, closer_label, "system",
                          f"Thread closed by {closer_label}: {reason}")
        db.mm_close_thread(thread["id"], closer.id if closer else self.bot.user.id, reason)

        user = await self.fetch_user(thread["user_id"])
        label = f"{user} ({thread['user_id']})" if user else str(thread["user_id"])
        log_channel = gc.get_log_channel(guild, "modmail-log-channel")
        if log_channel is not None:
            try:
                embed = discord.Embed(title=f"📬 Modmail #{thread['id']} closed", color=GREY, timestamp=discord.utils.utcnow())
                embed.add_field(name="Member", value=f"<@{thread['user_id']}> ({thread['user_id']})", inline=True)
                embed.add_field(name="Closed by", value=closer.mention if closer else "Automatically", inline=True)
                embed.add_field(name="Messages", value=str(db.mm_count_messages(thread["id"])), inline=True)
                embed.add_field(name="Reason", value=reason[:1024], inline=False)
                data = io.BytesIO(build_transcript(thread, label).encode("utf-8"))
                await log_channel.send(embed=embed, file=discord.File(data, filename=f"modmail-{thread['id']}.txt"),
                                       allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                logging.exception("couldn't post the modmail transcript")

        if notify_user and user is not None:
            try:
                text = "You closed this conversation." if by_user else "Staff closed this conversation."
                dm = discord.Embed(title="📪 Conversation closed", color=GREY,
                                   description=f"{text}\n**Reason:** {reason}\n\nSend me a message any time to start a new one.")
                dm.set_footer(text=guild.name)
                await user.send(embed=dm)
            except discord.HTTPException:
                pass

        channel = guild.get_channel(thread["channel_id"])
        if channel is not None and delete_channel:
            try:
                await channel.send(embed=discord.Embed(description=f"🔒 Closed by {closer.mention if closer else 'the bot'} - {reason}\nDeleting this channel in 5 seconds.", color=GREY))
                await asyncio.sleep(5)
                await channel.delete(reason=f"Modmail #{thread['id']} closed")
            except discord.HTTPException:
                pass

    async def close_from_interaction(self, interaction: discord.Interaction, reason: str):
        if not is_admin(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        thread = db.mm_get_open_by_channel(interaction.channel.id)
        if thread is None:
            return await interaction.response.send_message(embed=error("This isn't an open modmail thread."), ephemeral=True)
        await interaction.response.send_message(embed=discord.Embed(description="Closing the thread...", color=GREY))
        await self.close_thread(thread, interaction.guild, closer=interaction.user, reason=reason)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        """A thread channel deleted by hand: close the thread properly (transcript + tell the member)."""
        thread = db.mm_get_open_by_channel(channel.id)
        if thread is not None:
            await self.close_thread(thread, channel.guild, closer=None, reason="The thread channel was deleted.",
                                    delete_channel=False)

    @tasks.loop(minutes=30)
    async def autoclose(self):
        for guild in self.bot.guilds:
            try:
                days = int(db.get_setting(guild.id, AUTOCLOSE_KEY, DEFAULT_AUTOCLOSE_DAYS) or 0)
            except (TypeError, ValueError):
                days = DEFAULT_AUTOCLOSE_DAYS
            if days <= 0:
                continue
            cutoff = time.time() - days * 86400
            rows = db._fetch(
                "SELECT t.*, COALESCE((SELECT MAX(m.created_at) FROM modmail_messages m WHERE m.thread_id = t.id), "
                "t.created_at) AS last_activity FROM modmail_threads t WHERE t.guild_id=? AND t.status='open'",
                (guild.id,), many=True)
            for row in rows:
                if row["last_activity"] < cutoff:
                    try:
                        await self.close_thread(row, guild, closer=None,
                                                reason=f"Closed automatically after {days} day(s) without activity.")
                    except Exception:
                        logging.exception("modmail auto-close failed for thread %s", row["id"])

    @autoclose.before_loop
    async def _wait(self):
        await self.bot.wait_until_ready()

    # ---------------- claim / anonymous ----------------

    async def claim(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            return await interaction.response.send_message("Only staff can claim modmail threads.", ephemeral=True)
        thread = db.mm_get_open_by_channel(interaction.channel.id)
        if thread is None:
            return await interaction.response.send_message(embed=error("This isn't an open modmail thread."), ephemeral=True)
        if thread["claimed_by"]:
            return await interaction.response.send_message(
                embed=error(f"Already claimed by <@{thread['claimed_by']}>."), ephemeral=True)
        db.mm_update_thread(thread["id"], claimed_by=interaction.user.id)
        await interaction.response.send_message(embed=discord.Embed(
            description=f"🙋 {interaction.user.mention} claimed this thread.", color=GREEN))

    async def unclaim(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        thread = db.mm_get_open_by_channel(interaction.channel.id)
        if thread is None:
            return await interaction.response.send_message(embed=error("This isn't an open modmail thread."), ephemeral=True)
        if not thread["claimed_by"]:
            return await interaction.response.send_message(embed=error("This thread isn't claimed."), ephemeral=True)
        if thread["claimed_by"] != interaction.user.id and not is_admin(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        db.mm_update_thread(thread["id"], claimed_by=None)
        await interaction.response.send_message(embed=discord.Embed(description="🔓 Thread unclaimed.", color=GREY))

    async def toggle_anonymous(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        thread = db.mm_get_open_by_channel(interaction.channel.id)
        if thread is None:
            return await interaction.response.send_message(embed=error("This isn't an open modmail thread."), ephemeral=True)
        new = 0 if thread["anonymous"] else 1
        db.mm_update_thread(thread["id"], anonymous=new)
        await interaction.response.send_message(embed=discord.Embed(
            description=("🕶️ Replies are now **anonymous** - the member sees \"Staff Team\"." if new
                         else "👤 Replies now show **your name** to the member."), color=BLURPLE))

    # ---------------- /support-mode ----------------

    @app_commands.command(name="support-mode", description="[Admin] Choose how members get support: tickets, modmail or both")
    @app_commands.describe(mode="Tickets, modmail, or both (leave empty to see the current setting)",
                           autoclose_days="Close idle modmail threads after this many days (0 = never)",
                           modmail_label="Name of the modmail option in the ticket panel dropdown (default: Video Suggestion)")
    @app_commands.choices(mode=[
        app_commands.Choice(name="Tickets (panel button opens a private channel)", value=support.TICKETS),
        app_commands.Choice(name="Modmail (members DM the bot)", value=support.MODMAIL),
        app_commands.Choice(name="Both", value=support.BOTH),
    ])
    async def support_mode(self, interaction: discord.Interaction, mode: app_commands.Choice[str] = None,
                           autoclose_days: int = None, modmail_label: str = None):
        private = not getattr(interaction, "is_prefix", False)
        member, guild = interaction.user, interaction.guild
        if not (permissions.is_owner(member.id) or command_perms.granted() or member.guild_permissions.administrator):
            return await interaction.response.send_message(command_perms.NO_PERMS_MESSAGE, ephemeral=True)
        if autoclose_days is not None and not 0 <= autoclose_days <= 90:
            return await interaction.response.send_message(embed=error("Auto-close must be between 0 and 90 days."), ephemeral=private)

        if modmail_label is not None and not 1 <= len(modmail_label.strip()) <= 100:
            return await interaction.response.send_message(embed=error("The dropdown name must be 1-100 characters."), ephemeral=private)

        changed = []
        if modmail_label is not None:
            support.set_modmail_label(guild.id, modmail_label)
            changed.append("label")
        if mode is not None:
            support.set_mode(guild.id, mode.value)
            changed.append("mode")
        if autoclose_days is not None:
            db.set_setting(guild.id, AUTOCLOSE_KEY, autoclose_days)
            changed.append("auto-close")

        refreshed = 0
        if "mode" in changed or "label" in changed:
            tickets = self.bot.get_cog("Tickets")
            if tickets is not None:
                refreshed = await tickets.refresh_panels(guild)
        current = support.get_mode(guild.id)
        days = int(db.get_setting(guild.id, AUTOCLOSE_KEY, DEFAULT_AUTOCLOSE_DAYS) or 0)
        embed = discord.Embed(title="🛟 Support mode", color=GREEN if changed else BLURPLE,
                              description=f"Members get support through: **{support.LABELS[current]}**")
        embed.add_field(name="Tickets", value="✅ on - \"Support Ticket\" is in the dropdown" if support.allows_tickets(current)
                        else "⛔ off - not in the dropdown", inline=True)
        embed.add_field(name="Modmail", value="✅ on - in the dropdown, and members can DM the bot" if support.allows_modmail(current)
                        else "⛔ off - not in the dropdown, new DMs are ignored", inline=True)
        embed.add_field(name="Auto-close", value=f"{days} day(s) idle" if days else "off", inline=True)
        embed.add_field(name="Open now", value=f"{db.mm_count_open(guild.id)} modmail thread(s)", inline=True)
        embed.add_field(name="Dropdown name", value=f"🎬 {support.get_modmail_label(guild.id)}", inline=True)
        warnings = []
        if support.allows_modmail(current):
            if not guild.get_role(gc.get(guild.id, "admin-role")):
                warnings.append("No admin role is set (`/set`), so nobody but server administrators can see video suggestions.")
            if not guild.me.guild_permissions.manage_channels:
                warnings.append("I need **Manage Channels** to open modmail threads.")
        if warnings:
            embed.add_field(name="⚠️ Check", value="\n".join(warnings), inline=False)
        embed.set_footer(text=(f"Updated {refreshed} posted panel(s). " if refreshed else "")
                         + "Switching only affects new conversations.")
        await interaction.response.send_message(embed=embed, ephemeral=private)

    # ---------------- /modmail commands ----------------

    modmail_group = app_commands.Group(name="modmail", description="[Staff] Manage modmail threads")

    async def _thread_here(self, interaction: discord.Interaction):
        """The open thread for this channel, or None after replying with the reason."""
        if not is_admin(interaction.user):
            await interaction.response.send_message("you dont have the perms", ephemeral=True)
            return None
        thread = db.mm_get_open_by_channel(interaction.channel.id)
        if thread is None:
            await interaction.response.send_message(
                embed=error("Use this inside an open modmail thread channel."), ephemeral=True)
        return thread

    @modmail_group.command(name="close", description="Close this modmail thread (saves a transcript)")
    @app_commands.describe(reason="Shown to the member and saved in the transcript")
    async def mm_close(self, interaction: discord.Interaction, reason: str = None):
        await self.close_from_interaction(interaction, (reason or "").strip())

    @modmail_group.command(name="claim", description="Claim this thread - run it again to release your claim")
    async def mm_claim(self, interaction: discord.Interaction):
        thread = db.mm_get_open_by_channel(interaction.channel.id) if is_admin(interaction.user) else None
        if thread is not None and thread["claimed_by"] == interaction.user.id:
            return await self.unclaim(interaction)
        await self.claim(interaction)

    @modmail_group.command(name="anonymous", description="Toggle anonymous replies in this thread")
    async def mm_anonymous(self, interaction: discord.Interaction):
        await self.toggle_anonymous(interaction)

    @modmail_group.command(name="contact", description="Start a modmail thread with a member")
    @app_commands.describe(user="Mention or user ID", message="Optional first message to send them")
    async def mm_contact(self, interaction: discord.Interaction, user: discord.User, message: str = None):
        private = not getattr(interaction, "is_prefix", False)
        if not is_admin(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        guild = interaction.guild
        member = guild.get_member(user.id)
        if member is None:
            return await interaction.response.send_message(embed=error("That user isn't in this server."), ephemeral=private)
        if user.bot:
            return await interaction.response.send_message(embed=error("You can't open modmail with a bot."), ephemeral=private)
        existing = db.mm_get_open_by_user(guild.id, user.id)
        if existing is not None:
            ch = guild.get_channel(existing["channel_id"])
            return await interaction.response.send_message(
                embed=error(f"They already have an open thread{f': {ch.mention}' if ch else ''}."), ephemeral=private)
        await interaction.response.defer(ephemeral=private)
        opened = await self.open_thread(guild, member, opened_by=interaction.user)
        if opened is None:
            return await interaction.followup.send(embed=error("I couldn't create the channel - check my permissions and the category."), ephemeral=private)
        thread, channel, _dm_ok = opened
        if message:
            embed = discord.Embed(description=message, color=GREEN, timestamp=discord.utils.utcnow())
            embed.set_author(name="Staff Team" if thread["anonymous"] else interaction.user.display_name,
                             icon_url=interaction.user.display_avatar.url)
            embed.set_footer(text=guild.name)
            try:
                sent = await member.send(embed=embed)
                db.mm_add_message(thread["id"], interaction.user.id, str(interaction.user), "staff", message,
                                  dm_msg_id=sent.id)
                await channel.send(embed=discord.Embed(description=f"**First message sent:** {message}"[:4000], color=GREEN))
            except discord.HTTPException:
                await channel.send(embed=error("I couldn't DM them the first message - their DMs may be closed."))
        await interaction.followup.send(f"✅ Thread opened: {channel.mention}", ephemeral=private)

    @modmail_group.command(name="block", description="Block a member from modmail - run it again to unblock. No member = show the blocklist")
    @app_commands.describe(user="Mention or user ID (leave empty to see who is blocked)", reason="Why (staff-only)")
    async def mm_block(self, interaction: discord.Interaction, user: discord.User = None, reason: str = "No reason given"):
        private = not getattr(interaction, "is_prefix", False)
        if not is_admin(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        guild = interaction.guild
        if user is None:  # no member given: show the blocklist
            rows = db.mm_blocks(guild.id)
            body = "\n".join(f"<@{r['user_id']}> - {r['reason'] or 'no reason'} (by <@{r['staff_id']}>)" for r in rows)[:4000] \
                or "Nobody is blocked."
            return await interaction.response.send_message(
                embed=discord.Embed(title="🚫 Modmail blocklist", description=body, color=BLURPLE), ephemeral=private)
        if db.mm_get_block(guild.id, user.id):  # already blocked: this run unblocks them
            db.mm_unblock(guild.id, user.id)
            return await interaction.response.send_message(embed=discord.Embed(
                description=f"✅ {user.mention} can use modmail again.", color=GREEN), ephemeral=private)
        if permissions.is_owner(user.id) or user.id == interaction.user.id:
            return await interaction.response.send_message(embed=error("You can't block that user."), ephemeral=private)
        db.mm_block(guild.id, user.id, reason, interaction.user.id)
        thread = db.mm_get_open_by_user(guild.id, user.id)
        note = ""
        if thread is not None:
            note = " Their open thread was closed."
            asyncio.create_task(self.close_thread(thread, guild, closer=interaction.user,
                                                  reason="The member was blocked from modmail.", notify_user=False))
        await interaction.response.send_message(embed=discord.Embed(
            description=f"🚫 {user.mention} can no longer use modmail.{note}\n**Reason:** {reason}", color=RED), ephemeral=private)

    @modmail_group.command(name="history", description="A member's past modmail threads")
    @app_commands.describe(user="Mention or user ID")
    async def mm_history(self, interaction: discord.Interaction, user: discord.User):
        private = not getattr(interaction, "is_prefix", False)
        if not is_admin(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        rows = db.mm_threads_for_user(interaction.guild.id, user.id, limit=10)
        if not rows:
            return await interaction.response.send_message(embed=error(f"{user.mention} has no modmail history."), ephemeral=private)
        lines = []
        for r in rows:
            opened = discord.utils.format_dt(datetime.fromtimestamp(r["created_at"], timezone.utc), "d")
            if r["status"] == "open":
                lines.append(f"**#{r['id']}** · {opened} · 🟢 open")
            else:
                lines.append(f"**#{r['id']}** · {opened} · closed · {db.mm_count_messages(r['id'])} msgs"
                             f"{' · ' + r['close_reason'][:80] if r['close_reason'] else ''}")
        await interaction.response.send_message(embed=discord.Embed(
            title=f"📬 Modmail history - {user}", description="\n".join(lines)[:4000], color=BLURPLE), ephemeral=private)


async def setup(bot: commands.Bot):
    await bot.add_cog(Modmail(bot))
