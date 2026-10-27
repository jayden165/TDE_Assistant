import asyncio
import html
import io
import json
import re
import logging
from datetime import datetime, timezone
import discord
from discord import app_commands
from discord.ext import commands

from permissions import is_owner
import command_perms
import database as db
import guild_config as gc
import support
from cogs.applications import OFF_WORDS, parse_color, valid_url


def is_staff(member: discord.Member) -> bool:
    if is_owner(member.id):
        return True
    if command_perms.granted():
        return True
    role_ids = {r.id for r in member.roles}
    return gc.get(member.guild.id, "staff-role") in role_ids or gc.get(member.guild.id, "admin-role") in role_ids


def is_admin(member: discord.Member) -> bool:
    if is_owner(member.id):
        return True
    return gc.get(member.guild.id, "admin-role") in {r.id for r in member.roles}


def is_admin_perm(member: discord.Member) -> bool:
    if is_owner(member.id):
        return True
    if command_perms.granted():
        return True
    return member.guild_permissions.administrator


def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return re.sub(r"-+", "-", text).strip("-")[:20]


class TicketPanelView(discord.ui.View):
    """Posted by /panel - a single button that opens a private ticket."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Open a Ticket", style=discord.ButtonStyle.primary, emoji="🎫",
                       custom_id="ticket_panel_open")
    async def open_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog: "Tickets" = interaction.client.get_cog("Tickets")
        await cog.open_ticket(interaction)


TICKET_STYLE_KEY = "ticket_panel_style"   # JSON: title, description, color, image, thumbnail, footer, placeholder, show_list, ticket_label
TICKET_MSGS_KEY = "ticket_panel_msgs"     # JSON: [[channel_id, message_id], ...] of posted panels, so they can be refreshed
DEFAULT_TITLE = "SUPPORT TICKETS"
DEFAULT_TEXT = "Welcome to the Support Center for **{server}**, please open a ticket by selecting the type below."
DEFAULT_PLACEHOLDER = "Please select a ticket type..."
DEFAULT_COLOR = 0x2ECC71  # green bar, like the original panel

# The ticket types in the dropdown, in order: key -> (label, emoji, the two description lines, who can see it).
# Edit the text here to change them. Admin tickets are only visible to the admin role.
TICKET_TYPES = {
    "general": ("General Ticket", "ℹ️", ["Have a question? Need support?", "Open a General Ticket!"], "staff"),
    "report": ("Report Ticket", "❗", ["Need to report something in-game or on Discord?", "Open a Report Ticket!"], "staff"),
    "events": ("Events Ticket", "⚠️", ["Do you have a question or an issue pertaining to an Event?", "Open a Event Ticket!"], "staff"),
    "admin": ("Admin Ticket", "👑", ["Need to get in contact with management or have billing issues?", "Open an Admin Ticket!"], "admin"),
}


def get_style(guild_id: int) -> dict:
    try:
        return json.loads(db.get_setting(guild_id, TICKET_STYLE_KEY, "{}")) or {}
    except (ValueError, TypeError):
        return {}


def save_style(guild_id: int, style: dict):
    if style:
        db.set_setting(guild_id, TICKET_STYLE_KEY, json.dumps(style))
    else:
        db.delete_setting(guild_id, TICKET_STYLE_KEY)


def panel_entries(guild_id: int) -> list[tuple]:
    """(value, label, emoji, description lines) for each dropdown entry the current support mode allows:
    the ticket types, then modmail (named by the server, default "Video Suggestion")."""
    mode = support.get_mode(guild_id)
    entries = []
    if support.allows_tickets(mode):
        for key, (label, emoji, lines, _who) in TICKET_TYPES.items():
            entries.append((key, label, emoji, lines))
    if support.allows_modmail(mode):
        label = support.get_modmail_label(guild_id)
        entries.append(("modmail", label, "🎬", ["Got an idea for a video? Tell us all about it!",
                                                f"Send us your {label.lower()}!"]))
    return entries


def panel_options(guild_id: int) -> list[discord.SelectOption]:
    return [discord.SelectOption(label=label[:100], value=value, emoji=emoji, description=lines[0][:100])
            for value, label, emoji, lines in panel_entries(guild_id)]


def build_panel_embed(guild: discord.Guild) -> discord.Embed:
    """The support panel embed, using the server's customization (see /panel customize:True)."""
    style = get_style(guild.id)
    embed = discord.Embed(
        title=style.get("title") or DEFAULT_TITLE,
        color=int(style["color"], 16) if style.get("color") else DEFAULT_COLOR,
    )
    text = (style.get("description") or DEFAULT_TEXT).replace("{server}", guild.name)
    if style.get("show_list", True):
        blocks = [f"{emoji} **{label}:**\n" + "\n".join(lines) for _v, label, emoji, lines in panel_entries(guild.id)]
        if blocks:
            text += "\n\n" + "\n\n".join(blocks)
    embed.description = text[:4000]
    if style.get("image"):
        embed.set_image(url=style["image"])
    if style.get("thumbnail"):
        embed.set_thumbnail(url=style["thumbnail"])
    if style.get("footer"):
        embed.set_footer(text=style["footer"])
    return embed


class PanelSelect(discord.ui.Select):
    def __init__(self, options: list[discord.SelectOption], placeholder: str = DEFAULT_PLACEHOLDER):
        super().__init__(custom_id="ticket_panel_select", placeholder=placeholder,
                         options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        cog: "Tickets" = interaction.client.get_cog("Tickets")
        await cog.handle_panel_choice(interaction, self.values[0])


class TicketPanelSelectView(discord.ui.View):
    """The dropdown on the panel posted by /panel. Persistent; options and placeholder are rebuilt per server."""

    def __init__(self, options: list[discord.SelectOption] | None = None, guild_id: int = 0):
        super().__init__(timeout=None)
        if not options:  # the copy registered at startup only needs the custom_id to route clicks
            options = [discord.SelectOption(label="Ticket", value="general"),
                       discord.SelectOption(label="Modmail", value="modmail")]
        self.add_item(PanelSelect(options, get_style(guild_id).get("placeholder") or DEFAULT_PLACEHOLDER))


class PanelStyleModal(discord.ui.Modal, title="Customize the support panel"):
    """Title, text, colour, banner image and the dropdown's placeholder. The rest are options on the command."""

    def __init__(self, cog: "Tickets", style: dict, footer=None, thumbnail=None, show_list=None,
                 modmail_label_value=None):
        super().__init__()
        self.cog, self.footer, self.thumbnail, self.show_list = cog, footer, thumbnail, show_list
        self.modmail_label_value = modmail_label_value
        self.title_in = discord.ui.TextInput(label="Title", max_length=256, required=False,
                                             default=style.get("title") or DEFAULT_TITLE)
        self.desc_in = discord.ui.TextInput(label="Text above the options ({server} = name)", style=discord.TextStyle.paragraph,
                                            max_length=1500, required=False,
                                            default=style.get("description") or DEFAULT_TEXT)
        self.color_in = discord.ui.TextInput(label="Colour (hex, e.g. 5865F2)", max_length=7, required=False,
                                             default=style.get("color"), placeholder="blank = default blurple")
        self.image_in = discord.ui.TextInput(label="Banner image URL (optional)", max_length=400, required=False,
                                             default=style.get("image"), placeholder="https://...")
        self.placeholder_in = discord.ui.TextInput(label="Dropdown placeholder", max_length=100, required=False,
                                                   default=style.get("placeholder") or DEFAULT_PLACEHOLDER)
        for item in (self.title_in, self.desc_in, self.color_in, self.image_in, self.placeholder_in):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        async def fail(text: str):
            await interaction.response.send_message(
                embed=discord.Embed(title="❌ Nothing was saved", description=text, color=discord.Color.red()), ephemeral=True)

        guild = interaction.guild
        style = get_style(guild.id)
        color_text, image = str(self.color_in.value).strip(), str(self.image_in.value).strip()
        if color_text and parse_color(color_text) is None:
            return await fail("The colour needs to be a 6-digit hex code like `5865F2` or `#ff8800`.")
        if image and not valid_url(image):
            return await fail("The banner image must be a link starting with `http://` or `https://`.")
        if self.thumbnail and self.thumbnail.lower() not in OFF_WORDS and not valid_url(self.thumbnail):
            return await fail("The thumbnail must be a link starting with `http://` or `https://`.")

        def put(key, value):
            if value:
                style[key] = value
            else:
                style.pop(key, None)

        title, desc, placeholder = (str(self.title_in.value).strip(), str(self.desc_in.value).strip(),
                                    str(self.placeholder_in.value).strip())
        put("title", title if title != DEFAULT_TITLE else "")
        put("description", desc if desc != DEFAULT_TEXT else "")
        put("color", parse_color(color_text) if color_text else "")
        put("image", image)
        put("placeholder", placeholder if placeholder != DEFAULT_PLACEHOLDER else "")
        if self.footer is not None:
            put("footer", "" if self.footer.lower() in OFF_WORDS else self.footer)
        if self.thumbnail is not None:
            put("thumbnail", "" if self.thumbnail.lower() in OFF_WORDS else self.thumbnail)
        if self.show_list is not None:
            style["show_list"] = self.show_list
        if self.modmail_label_value:
            support.set_modmail_label(guild.id, self.modmail_label_value)
        save_style(guild.id, style)

        updated = await self.cog.refresh_panels(guild)
        note = (f"✅ Saved. Updated {updated} posted panel(s)." if updated
                else "✅ Saved. Post the panel with `/panel` to use it.")
        await interaction.response.send_message(content=f"{note} Preview:", embed=build_panel_embed(guild), ephemeral=True)


class CloseConfirmView(discord.ui.View):
    """'Are you sure?' shown to whoever pressed Close."""

    def __init__(self, cog: "Tickets", requester_id: int, reason: str = None):
        super().__init__(timeout=60)
        self.cog, self.requester_id, self.reason = cog, requester_id, reason

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id and not is_admin(interaction.user):
            await interaction.response.send_message("you dont have the perms", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, emoji="🔒")
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.perform_close(interaction, self.reason)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Close cancelled.", view=None)


class ClosedTicketView(discord.ui.View):
    """Posted when a ticket is closed: get the transcript, reopen it, or delete the channel."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Transcript", style=discord.ButtonStyle.secondary, emoji="📑", custom_id="ticket_transcript")
    async def transcript(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.client.get_cog("Tickets").send_transcript(interaction)

    @discord.ui.button(label="Open", style=discord.ButtonStyle.success, emoji="🔓", custom_id="ticket_reopen")
    async def reopen(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.client.get_cog("Tickets").reopen(interaction)

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger, emoji="⛔", custom_id="ticket_delete")
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.client.get_cog("Tickets").delete_ticket(interaction)


class TicketControlView(discord.ui.View):
    """Posted inside each ticket channel. Persistent."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="ticket_close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.client.get_cog("Tickets").close(interaction)

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.primary, emoji="🙋", custom_id="ticket_claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.client.get_cog("Tickets").claim(interaction)

    @discord.ui.button(label="Unclaim", style=discord.ButtonStyle.secondary, custom_id="ticket_unclaim")
    async def unclaim(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.client.get_cog("Tickets").unclaim(interaction)

    @discord.ui.button(label="Close Request", style=discord.ButtonStyle.secondary, emoji="🔔", custom_id="ticket_close_request")
    async def close_request(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.client.get_cog("Tickets").close_request(interaction)


class ApplicationTicketView(TicketControlView):
    """The same four buttons, plus Accept / Deny on a second row - used on the opening message of an
    application ticket. Accept gives the application's role (if one is set) and DMs the applicant."""

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, emoji="✅", custom_id="app_ticket_accept", row=1)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.client.get_cog("Applications").prompt_decision(interaction, "Accepted")

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, emoji="❌", custom_id="app_ticket_deny", row=1)
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.client.get_cog("Applications").prompt_decision(interaction, "Denied")


class CloseRequestView(discord.ui.View):
    """Under a staff member's close request: the ticket's owner (or staff) closes it, or keeps it open."""

    def __init__(self):
        super().__init__(timeout=None)

    async def _allowed(self, interaction: discord.Interaction) -> bool:
        ticket = db.get_ticket(interaction.channel_id)
        if ticket and (interaction.user.id == ticket["user_id"] or is_staff(interaction.user)):
            return True
        await interaction.response.send_message("This request isn't for you.", ephemeral=True)
        return False

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="ticket_cr_approve")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await self._allowed(interaction):
            await interaction.client.get_cog("Tickets").perform_close(interaction, "Close request accepted")

    @discord.ui.button(label="Keep open", style=discord.ButtonStyle.secondary, custom_id="ticket_cr_deny")
    async def keep(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await self._allowed(interaction):
            await interaction.response.edit_message(
                content=None, view=None,
                embed=discord.Embed(description=f"🔔 {interaction.user.mention} wants to keep this ticket open.",
                                    color=discord.Color.greyple()))


async def build_transcript_html(channel: discord.TextChannel, ticket) -> str:
    """A readable, self-contained HTML transcript of the channel (like Ticket Tool's)."""
    esc = html.escape
    rows = []
    async for msg in channel.history(limit=None, oldest_first=True):
        body = esc(msg.content or "").replace("\n", "<br>")
        extra = ""
        for e in msg.embeds:
            parts = []
            if e.title:
                parts.append(f"<b>{esc(e.title)}</b>")
            if e.description:
                parts.append(esc(e.description).replace("\n", "<br>"))
            for f in e.fields:
                parts.append(f"<b>{esc(f.name)}</b><br>{esc(f.value).replace(chr(10), '<br>')}")
            if parts:
                extra += '<div class="embed">' + "<br><br>".join(parts) + "</div>"
        for a in msg.attachments:
            extra += f'<div class="att">📎 <a href="{esc(a.url)}">{esc(a.filename)}</a></div>'
        stamp = msg.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
        rows.append(
            f'<div class="msg"><img class="av" src="{esc(msg.author.display_avatar.url)}">'
            f'<div><span class="au">{esc(msg.author.display_name)}</span>'
            f'{"<span class=bot>BOT</span>" if msg.author.bot else ""} <span class="ts">{stamp}</span>'
            f'<div class="tx">{body}</div>{extra}</div></div>')
    opened = datetime.fromtimestamp(ticket["created_at"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'><title>" + esc(channel.name) + "</title><style>"
        "body{background:#313338;color:#dbdee1;font-family:Arial,sans-serif;margin:0;padding:24px}"
        "h1{font-size:20px;margin:0 0 4px}.meta{color:#949ba4;font-size:13px;margin-bottom:20px}"
        ".msg{display:flex;gap:12px;margin:14px 0}.av{width:40px;height:40px;border-radius:50%}"
        ".au{font-weight:bold;color:#fff}.ts{color:#949ba4;font-size:12px;margin-left:6px}"
        ".bot{background:#5865f2;color:#fff;font-size:10px;padding:1px 4px;border-radius:3px;margin-left:6px}"
        ".tx{margin-top:2px;white-space:normal}.embed{border-left:4px solid #5865f2;background:#2b2d31;"
        "padding:8px 12px;margin-top:6px;border-radius:4px}.att{margin-top:4px}a{color:#00a8fc}"
        "</style></head><body>"
        f"<h1>#{esc(channel.name)}</h1><div class='meta'>{esc(channel.guild.name)} - opened {opened} - "
        f"opened by user {ticket['user_id']}</div>" + "".join(rows) + "</body></html>")


class Tickets(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._panels_refreshed = False
        self.bot.add_view(TicketPanelView())        # panels posted before the dropdown existed
        self.bot.add_view(TicketPanelSelectView())
        self.bot.add_view(TicketControlView())
        self.bot.add_view(ApplicationTicketView())
        self.bot.add_view(CloseRequestView())
        self.bot.add_view(ClosedTicketView())

    @commands.Cog.listener()
    async def on_ready(self):
        """Once per start: re-render the panels this bot posted, so they match the current settings."""
        if self._panels_refreshed:
            return
        self._panels_refreshed = True
        for guild in self.bot.guilds:
            try:
                await self.refresh_panels(guild)
            except Exception:
                logging.exception("couldn't refresh panels for guild %s", guild.id)

    # ---------------- panel setup ----------------

    @app_commands.command(name="panel", description="[Staff] Post the support panel here - add an option below to change how it looks [Admin]")
    @app_commands.describe(
        customize="Open the form that changes how the panel looks (admin)",
        modmail_label="Name of the modmail option in the dropdown (default: Video Suggestion)",
        footer="Small text at the bottom (use 'off' to remove)",
        thumbnail="Small image link in the corner (use 'off' to remove)",
        show_list="Describe each option under the text (default: yes)",
        reset="Put the panel back to its default look",
    )
    async def panel(self, interaction: discord.Interaction, customize: bool = False, modmail_label: str = None,
                    footer: str = None, thumbnail: str = None, show_list: bool = None, reset: bool = False):
        styling = customize or reset or any(v is not None for v in (modmail_label, footer, thumbnail, show_list))
        if not styling:  # plain /panel posts the panel
            if not is_staff(interaction.user):
                return await interaction.response.send_message("you dont have the perms", ephemeral=True)
            return await self.post_support_panel(interaction)
        if not is_admin(interaction.user) and not is_owner(interaction.user.id):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        if reset:
            save_style(interaction.guild.id, {})
            await interaction.response.defer(ephemeral=True)
            updated = await self.refresh_panels(interaction.guild)
            return await interaction.followup.send(
                content="♻️ The panel is back to its default look" + (f" (updated {updated} posted panel(s))." if updated else "."),
                embed=build_panel_embed(interaction.guild), ephemeral=True)
        await interaction.response.send_modal(PanelStyleModal(
            self, get_style(interaction.guild.id), footer=footer, thumbnail=thumbnail, show_list=show_list,
            modmail_label_value=modmail_label))

    async def post_support_panel(self, interaction: discord.Interaction):
        guild = interaction.guild
        message = await interaction.channel.send(embed=build_panel_embed(guild),
                                                 view=TicketPanelSelectView(panel_options(guild.id), guild.id))
        # remember it so customizing (or /support-mode) can keep it up to date
        try:
            refs = json.loads(db.get_setting(guild.id, TICKET_MSGS_KEY, "[]"))
        except (ValueError, TypeError):
            refs = []
        refs.append([message.channel.id, message.id])
        db.set_setting(guild.id, TICKET_MSGS_KEY, json.dumps(refs[-10:]))
        await interaction.response.send_message("Panel posted.", ephemeral=True)

    async def refresh_panels(self, guild: discord.Guild) -> int:
        """Re-renders every support panel this bot posted, so changes show up without re-posting.
        Returns how many were updated; panels whose message is gone are forgotten."""
        try:
            refs = json.loads(db.get_setting(guild.id, TICKET_MSGS_KEY, "[]"))
        except (ValueError, TypeError):
            return 0
        kept, updated = [], 0
        for channel_id, message_id in refs:
            channel = guild.get_channel(channel_id)
            if channel is None:
                continue
            try:
                message = await channel.fetch_message(message_id)
                await message.edit(embed=build_panel_embed(guild),
                                   view=TicketPanelSelectView(panel_options(guild.id), guild.id))
                kept.append([channel_id, message_id])
                updated += 1
            except discord.NotFound:
                continue
            except discord.HTTPException:
                kept.append([channel_id, message_id])
        db.set_setting(guild.id, TICKET_MSGS_KEY, json.dumps(kept))
        return updated

    async def handle_panel_choice(self, interaction: discord.Interaction, choice: str):
        """A pick from the panel dropdown: 'ticket' opens a ticket, 'modmail' starts a modmail thread."""
        if choice == "modmail":
            modmail = interaction.client.get_cog("Modmail")
            if modmail is None:
                await interaction.response.send_message("That option isn't available right now.", ephemeral=True)
            else:
                await modmail.start_from_panel(interaction)
        else:
            await self.open_ticket(interaction, choice if choice in TICKET_TYPES else "general")
        try:  # put the dropdown back to its placeholder so the same option can be picked again
            await interaction.message.edit(view=TicketPanelSelectView(panel_options(interaction.guild.id), interaction.guild.id))
        except discord.HTTPException:
            pass

    # ---------------- ticket creation ----------------

    def open_ticket_channel(self, guild: discord.Guild, user_id: int):
        """The user's open ticket channel, or None. Tickets whose channel was deleted by hand are closed out."""
        for row in db.get_open_tickets_for_user(guild.id, user_id):
            channel = guild.get_channel(row["channel_id"])
            if channel is not None:
                return channel
            db.close_ticket(row["channel_id"])
        return None

    async def create_ticket_channel(self, guild: discord.Guild, user: discord.Member,
                                     name_override: str = None, extra_pings: list = None, ticket_type: str = None,
                                     opening_embeds: list = None, application: bool = False, topic: str = None):
        """Shared ticket-channel creation used by the panel and applications."""
        label, emoji, who = "Ticket", "🎫", "staff"
        if ticket_type in TICKET_TYPES:
            label, emoji, _lines, who = TICKET_TYPES[ticket_type]
        ticket_category_id = gc.get(guild.id, "ticket-category")
        cat_channel = guild.get_channel(ticket_category_id) if ticket_category_id else None
        if ticket_category_id and not isinstance(cat_channel, discord.CategoryChannel):
            # The ID is set but doesn't resolve to a category (wrong ID, or the bot can't see it).
            # Log loudly instead of silently dumping tickets at the top of the server.
            logging.warning(
                "Ticket category %s did not resolve to a category channel in guild %s - ticket will be "
                "created without a category. Check TICKET_CATEGORY_ID in .env and the bot's permissions.",
                ticket_category_id, guild.id,
            )
            cat_channel = None

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        staff_role = guild.get_role(gc.get(guild.id, "staff-role"))
        admin_role = guild.get_role(gc.get(guild.id, "admin-role"))
        if who == "admin":
            staff_role = None  # admin tickets (management / billing) are only visible to the admin role
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        if admin_role:
            overwrites[admin_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

        number = db.count_tickets(guild.id) + 1
        chan_name = name_override or f"{ticket_type or 'ticket'}-{number:04d}"  # e.g. general-0001, like Ticket Tool
        try:
            channel = await guild.create_text_channel(
                name=chan_name[:100],
                category=cat_channel,
                overwrites=overwrites,
                topic=topic or f"{label} | opened by {user} ({user.id})",
                reason=f"{label} opened by {user}",
            )
        except discord.Forbidden:
            return None

        db.create_ticket(channel.id, guild.id, user.id, ticket_type)

        ping = user.mention
        if extra_pings:
            ping += " " + " ".join(extra_pings)
        elif who == "admin" and admin_role:
            ping += f" {admin_role.mention}"
        elif staff_role:
            ping += f" {staff_role.mention}"
        if opening_embeds:
            # e.g. an application: the opening message IS the application, with the control buttons.
            # Discord caps a message at 6000 characters of embeds, so longer ones continue in follow-ups.
            await channel.send(content=ping, embed=opening_embeds[0],
                               view=ApplicationTicketView() if application else TicketControlView())
            for extra in opening_embeds[1:]:
                await channel.send(embed=extra)
            return channel
        embed = discord.Embed(
            title=f"{emoji} {label}",
            description=f"Hello {user.mention}!\nThank you for contacting support. Please describe your issue and "
                         f"wait for a response - staff will be with you shortly.\n\nTo close this ticket, press 🔒.",
            color=discord.Color(DEFAULT_COLOR),
        )
        await channel.send(content=ping, embed=embed, view=TicketControlView())
        return channel

    async def open_ticket(self, interaction: discord.Interaction, ticket_type: str = "general"):
        if not support.allows_tickets(support.get_mode(interaction.guild.id)):
            # An old panel is still posted, but this server switched to modmail.
            return await interaction.response.send_message(
                "Tickets are turned off right now - pick the other option in the dropdown, or send "
                f"{interaction.client.user.mention} a direct message and staff will reply there.", ephemeral=True,
            )
        existing = self.open_ticket_channel(interaction.guild, interaction.user.id)
        if existing is not None:  # one ticket at a time per member
            return await interaction.response.send_message(
                f"You already have an open ticket: {existing.mention}. Please use it - or ask for it to be closed - "
                "before opening another.", ephemeral=True,
            )
        if TICKET_TYPES.get(ticket_type, ("", "", [], "staff"))[3] == "admin" and not gc.get(interaction.guild.id, "admin-role"):
            return await interaction.response.send_message(
                "Admin tickets aren't set up on this server yet (no admin role).", ephemeral=True)
        channel = await self.create_ticket_channel(interaction.guild, interaction.user, ticket_type=ticket_type)
        if channel is None:
            return await interaction.response.send_message(
                "I don't have permission to create ticket channels. Contact an admin.", ephemeral=True,
            )

        await interaction.response.send_message(
            f"\u2705 Your ticket has been created: {channel.mention}", ephemeral=True,
        )

    # ---------------- claim system ----------------

    async def claim(self, interaction: discord.Interaction):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("Only staff can claim tickets.", ephemeral=True)
        ticket = db.get_ticket(interaction.channel_id)
        if not ticket:
            return await interaction.response.send_message("This isn't a ticket channel.", ephemeral=True)
        if ticket["claimed_by"]:
            return await interaction.response.send_message("This ticket is already claimed.", ephemeral=True)

        db.claim_ticket(interaction.channel_id, interaction.user.id)
        await self._lock_to_claimer(interaction.channel, interaction.user.id)
        await interaction.response.send_message(f"🔒 Ticket claimed by {interaction.user.mention}. "
                                                  f"Other staff can no longer type here until it's unclaimed.")

    async def unclaim(self, interaction: discord.Interaction):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("Only staff can unclaim tickets.", ephemeral=True)
        ticket = db.get_ticket(interaction.channel_id)
        if not ticket:
            return await interaction.response.send_message("This isn't a ticket channel.", ephemeral=True)
        if not ticket["claimed_by"]:
            return await interaction.response.send_message("This ticket isn't claimed.", ephemeral=True)
        if ticket["claimed_by"] != interaction.user.id and not is_admin(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)

        db.unclaim_ticket(interaction.channel_id)
        await self._unlock_staff(interaction.channel)
        await interaction.response.send_message("🔓 Ticket unclaimed. Any staff member may respond now.")

    async def _lock_to_claimer(self, channel: discord.TextChannel, claimer_id: int):
        guild = channel.guild
        staff_role = guild.get_role(gc.get(guild.id, "staff-role"))
        if staff_role:
            await channel.set_permissions(staff_role, send_messages=False, view_channel=True, read_message_history=True)
        claimer = guild.get_member(claimer_id)
        if claimer:
            await channel.set_permissions(claimer, send_messages=True, view_channel=True, read_message_history=True)
        # Admins always retain access via their role overwrite (not restricted here).

    async def _unlock_staff(self, channel: discord.TextChannel):
        guild = channel.guild
        staff_role = guild.get_role(gc.get(guild.id, "staff-role"))
        if staff_role:
            await channel.set_permissions(staff_role, send_messages=True, view_channel=True, read_message_history=True)

    # ---------------- close request (staff -> ticket owner) ----------------

    async def close_request(self, interaction: discord.Interaction):
        """Staff ask the ticket's owner to close it; the owner presses Close or Keep open."""
        ticket = db.get_ticket(interaction.channel_id)
        if not ticket:
            return await interaction.response.send_message("This isn't a ticket channel.", ephemeral=True)
        if ticket["status"] != "open":
            return await interaction.response.send_message("This ticket is already closed.", ephemeral=True)
        if not is_staff(interaction.user):
            return await interaction.response.send_message(
                "Only staff send close requests - you can close your own ticket with the **Close** button.", ephemeral=True)
        embed = discord.Embed(
            title="🔔 Close request",
            description=f"{interaction.user.mention} has asked to close this ticket. If you have nothing more to add, "
                        "press **Close**. Otherwise press **Keep open** and let us know what you still need.",
            color=discord.Color.orange())
        await interaction.response.send_message(content=f"<@{ticket['user_id']}>", embed=embed, view=CloseRequestView(),
                                                allowed_mentions=discord.AllowedMentions(users=True))

    # ---------------- close / reopen / delete ----------------

    @staticmethod
    async def _respond(interaction: discord.Interaction, text: str):
        """Answer a button press by replacing its message, or a slash command with a normal reply."""
        if getattr(interaction, "type", None) == discord.InteractionType.component:
            await interaction.response.edit_message(content=text, embed=None, view=None)
        else:
            await interaction.response.send_message(text)

    async def close(self, interaction: discord.Interaction):
        ticket = db.get_ticket(interaction.channel_id)
        if not ticket:
            return await interaction.response.send_message("This isn't a ticket channel.", ephemeral=True)
        if ticket["status"] != "open":
            return await interaction.response.send_message("This ticket is already closed.", ephemeral=True)
        if not is_staff(interaction.user) and interaction.user.id != ticket["user_id"]:
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        await interaction.response.send_message(
            "Are you sure you want to close this ticket?",
            view=CloseConfirmView(self, interaction.user.id), ephemeral=True)

    async def perform_close(self, interaction: discord.Interaction, reason: str = None):
        channel, guild = interaction.channel, interaction.guild
        ticket = db.get_ticket(channel.id)
        if not ticket or ticket["status"] != "open":
            return await self._respond(interaction, "This ticket is already closed.")
        reason = (reason or "").strip() or "No reason given"
        await self._respond(interaction, "🔒 Closing the ticket and saving the transcript...")

        document = (await build_transcript_html(channel, ticket)).encode("utf-8")
        filename = f"transcript-{channel.name}.html"
        db.close_ticket(channel.id)
        opener = guild.get_member(ticket["user_id"])
        claimed = f"<@{ticket['claimed_by']}>" if ticket["claimed_by"] else "Nobody"

        # transcript + summary to the log channel
        log_channel = guild.get_channel(gc.get(guild.id, "transcript-channel") or 0)
        if log_channel is not None:
            log = discord.Embed(title="🔒 Ticket Closed", color=discord.Color.dark_grey(), timestamp=discord.utils.utcnow())
            log.add_field(name="Ticket", value=channel.name, inline=True)
            log.add_field(name="Opened by", value=f"<@{ticket['user_id']}>", inline=True)
            log.add_field(name="Closed by", value=interaction.user.mention, inline=True)
            log.add_field(name="Opened", value=discord.utils.format_dt(datetime.fromtimestamp(ticket["created_at"], timezone.utc), "f"), inline=True)
            log.add_field(name="Claimed by", value=claimed, inline=True)
            log.add_field(name="Reason", value=reason[:1024], inline=False)
            try:
                await log_channel.send(embed=log, file=discord.File(io.BytesIO(document), filename=filename),
                                       allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                logging.exception("couldn't post the ticket transcript")

        # transcript to the ticket owner's DMs
        if opener is not None:
            try:
                dm = discord.Embed(title="🔒 Your ticket was closed", color=discord.Color.dark_grey(),
                                   description=f"**{channel.name}** in **{guild.name}** was closed.\n**Reason:** {reason}")
                await opener.send(embed=dm, file=discord.File(io.BytesIO(document), filename=filename))
            except discord.HTTPException:
                pass  # DMs closed

        # the owner loses access; the channel stays for staff until it's deleted or reopened
        if opener is not None:
            try:
                await channel.set_permissions(opener, overwrite=None, reason="Ticket closed")
            except discord.HTTPException:
                pass
        suffix = channel.name.rsplit("-", 1)[-1]
        if re.fullmatch(r"\d{4}", suffix):
            try:
                await channel.edit(name=f"closed-{suffix}", reason="Ticket closed")
            except discord.HTTPException:
                pass  # channel renames are rate limited

        embed = discord.Embed(
            title="🔒 Ticket Closed",
            description=f"Closed by {interaction.user.mention}\n**Reason:** {reason}"
                        + (f"\n\nTranscript saved to {log_channel.mention}." if log_channel else ""),
            color=discord.Color.dark_grey())
        await channel.send(embed=embed, view=ClosedTicketView())

    async def reopen(self, interaction: discord.Interaction):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        channel, guild = interaction.channel, interaction.guild
        ticket = db.get_ticket(channel.id)
        if not ticket or ticket["status"] != "closed":
            return await interaction.response.send_message("This ticket isn't closed.", ephemeral=True)
        db.reopen_ticket(channel.id)
        opener = guild.get_member(ticket["user_id"])
        if opener is not None:
            try:
                await channel.set_permissions(opener, view_channel=True, send_messages=True, read_message_history=True,
                                              reason="Ticket reopened")
            except discord.HTTPException:
                pass
        suffix = channel.name.rsplit("-", 1)[-1]
        if channel.name.startswith("closed-") and re.fullmatch(r"\d{4}", suffix):
            try:
                await channel.edit(name=f"{ticket['ticket_type'] or 'ticket'}-{suffix}", reason="Ticket reopened")
            except discord.HTTPException:
                pass
        await interaction.response.edit_message(view=None)
        await channel.send(content=opener.mention if opener else None,
                           embed=discord.Embed(description=f"🔓 Ticket reopened by {interaction.user.mention}.",
                                               color=discord.Color.green()))

    async def delete_ticket(self, interaction: discord.Interaction):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        await interaction.response.send_message("🗑️ This ticket will be deleted in 5 seconds...")
        await asyncio.sleep(5)
        try:
            await interaction.channel.delete(reason=f"Ticket deleted by {interaction.user}")
        except discord.HTTPException:
            pass

    async def send_transcript(self, interaction: discord.Interaction):
        ticket = db.get_ticket(interaction.channel_id)
        if not ticket:
            return await interaction.response.send_message("This isn't a ticket channel.", ephemeral=True)
        if not is_staff(interaction.user) and interaction.user.id != ticket["user_id"]:
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        document = (await build_transcript_html(interaction.channel, ticket)).encode("utf-8")
        await interaction.followup.send("📑 Here's the transcript so far:", ephemeral=True,
                                        file=discord.File(io.BytesIO(document), filename=f"transcript-{interaction.channel.name}.html"))

    # ---------------- /ticket (manage the ticket you're in) ----------------

    ticket_group = app_commands.Group(name="ticket", description="Manage the ticket channel you're in")

    async def _ticket_here(self, interaction: discord.Interaction, staff_only: bool = True):
        ticket = db.get_ticket(interaction.channel_id)
        if not ticket:
            await interaction.response.send_message("Use this inside a ticket channel.", ephemeral=True)
            return None
        allowed = is_staff(interaction.user) or (not staff_only and interaction.user.id == ticket["user_id"])
        if not allowed:
            await interaction.response.send_message("you dont have the perms", ephemeral=True)
            return None
        return ticket

    @ticket_group.command(name="close", description="Close this ticket (you can add a reason)")
    @app_commands.describe(reason="Shown in the transcript log and sent to the ticket's owner")
    async def t_close(self, interaction: discord.Interaction, reason: str = None):
        if await self._ticket_here(interaction, staff_only=False) is None:
            return
        await self.perform_close(interaction, reason)

    @ticket_group.command(name="member", description="[Staff] Add someone to this ticket - or take them out with remove:True")
    @app_commands.describe(user="Who to add or remove", remove="Remove them from the ticket instead of adding them")
    async def t_member(self, interaction: discord.Interaction, user: discord.Member, remove: bool = False):
        ticket = await self._ticket_here(interaction)
        if ticket is None:
            return
        if not remove:
            await interaction.channel.set_permissions(user, view_channel=True, send_messages=True, read_message_history=True,
                                                      reason=f"Added to the ticket by {interaction.user}")
            return await interaction.response.send_message(embed=discord.Embed(
                description=f"✅ {user.mention} was added to this ticket.", color=discord.Color.green()))
        if user.id == ticket["user_id"]:
            return await interaction.response.send_message("You can't remove the ticket's owner - close it instead.", ephemeral=True)
        await interaction.channel.set_permissions(user, overwrite=None, reason=f"Removed from the ticket by {interaction.user}")
        await interaction.response.send_message(embed=discord.Embed(
            description=f"✅ {user.mention} was removed from this ticket.", color=discord.Color.orange()))

    @ticket_group.command(name="rename", description="[Staff] Rename this ticket channel")
    @app_commands.describe(name="The new channel name")
    async def t_rename(self, interaction: discord.Interaction, name: str):
        if await self._ticket_here(interaction) is None:
            return
        new = slugify(name) or "ticket"
        try:
            await interaction.channel.edit(name=new, reason=f"Renamed by {interaction.user}")
        except discord.HTTPException:
            return await interaction.response.send_message(
                "Couldn't rename it - Discord limits channel renames to 2 per 10 minutes.", ephemeral=True)
        await interaction.response.send_message(embed=discord.Embed(
            description=f"✏️ Renamed to **{new}**.", color=discord.Color.green()))

    @ticket_group.command(name="transcript", description="[Staff] Get a transcript of this ticket so far")
    async def t_transcript(self, interaction: discord.Interaction):
        if await self._ticket_here(interaction) is None:
            return
        await self.send_transcript(interaction)


async def setup(bot: commands.Bot):
    await bot.add_cog(Tickets(bot))
