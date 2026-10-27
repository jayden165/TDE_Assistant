import asyncio
import copy
import hashlib
import math
import json
import logging
import re
import discord
from discord import app_commands
from discord.ext import commands

from permissions import is_owner
import command_perms
import database as db
import guild_config as gc

# Fallback defaults for any server that hasn't customized its applications yet - keyed by
# name -> {"emoji": ..., "questions": [...]}. A server's real config lives in the generic
# settings table under the "application_types" key as JSON, see get_types()/save_types().
STAFF_QUESTIONS = [
    "What would you do if someone joined the server with a NSFW profile picture, name, or bio??",
    "How would you use sapphire to ban someone?",
    "What is sapphire's prefix command",
    "Do you have any experience moderating discord before?",
    "What would you do if someone sent the Mr. Beast Crypto Exitscam?",
]

DEFAULT_APPLICATIONS = {
    "Staff Team": {
        "emoji": "🛡️",
        "description": "Apply to join the staff team",
        "dm": True,  # the questions are asked in the applicant's DMs instead of a pop-up form
        "questions": STAFF_QUESTIONS,
    },
    "Developer": {
        "emoji": "💻",
        "questions": [
            "How old are you?",
            "What is your timezone?",
            "How active can you be weekly?",
            "Do you have any previous dev experience? If yes, please explain where and what position you had.",
            "Why do you want to become a developer here?",
            "Why should we choose you?",
            "How would you handle a bot bugging?",
            "How would you handle a bot having a bug where a user can use a command they shouldn't have access to?",
            "How would you handle a community ad that breaks the server rules/Discord TOS?",
            "Is there anything else we should know about you?",
        ],
    },
}

QUESTIONS_PER_PAGE = 5   # a Discord form holds at most 5 inputs, so long applications are asked in pages
MAX_APPLICATION_TYPES = 25  # one button each on the panel (5 rows of 5)
MAX_QUESTIONS = 20
MAX_DESCRIPTION = 100       # shown under the name on the panel
MAX_QUESTION_LENGTH = 100   # form applications: a question is the form field's label (45 chars) and placeholder (100 chars)
MAX_DM_QUESTION_LENGTH = 256  # DM applications can use longer questions
PAGE_BUDGET = 5000          # characters per review embed (Discord's limit is 6000 per message)


def is_staff(member: discord.Member) -> bool:
    if is_owner(member.id):
        return True
    if command_perms.granted():
        return True
    role_ids = {r.id for r in member.roles}
    return gc.get(member.guild.id, "staff-role") in role_ids or gc.get(member.guild.id, "admin-role") in role_ids


def is_admin_perm(member: discord.Member) -> bool:
    if is_owner(member.id):
        return True
    if command_perms.granted():
        return True
    return member.guild_permissions.administrator


def get_types(guild_id: int) -> dict:
    """This server's application types, or a COPY of the built-in defaults if it hasn't
    customized any. Must be a copy: callers commonly do `types = get_types(...)`, mutate
    the dict in place, then save_types(guild_id, types) - if this ever returned the live
    DEFAULT_APPLICATIONS object, the first server to customize anything would permanently
    corrupt the fallback every other unconfigured server sees, for the life of the process."""
    raw = db.get_setting(guild_id, "application_types")
    if raw:
        try:
            data = json.loads(raw)
            if data:
                return data
        except (json.JSONDecodeError, TypeError):
            pass
    return copy.deepcopy(DEFAULT_APPLICATIONS)


def save_types(guild_id: int, types: dict):
    db.set_setting(guild_id, "application_types", json.dumps(types))


PANEL_STYLE_KEY = "app_panel_style"   # JSON: title, description, color, image, thumbnail, footer, placeholder, show_list
PANEL_MSGS_KEY = "app_panel_msgs"     # JSON: [[channel_id, message_id], ...] of posted panels, so they can be refreshed
DEFAULT_PANEL_TITLE = "📋 Applications"
DEFAULT_PANEL_TEXT = ("Click a button below to apply. You'll answer the questions (in a form, or in your DMs for "
                      "some applications), then a private ticket is opened with the staff team.")
BUTTON_STYLES = {"blue": discord.ButtonStyle.primary, "grey": discord.ButtonStyle.secondary,
                 "gray": discord.ButtonStyle.secondary, "green": discord.ButtonStyle.success,
                 "red": discord.ButtonStyle.danger}
OFF_WORDS = {"none", "off", "clear", "remove", "-"}


def get_style(guild_id: int) -> dict:
    try:
        return json.loads(db.get_setting(guild_id, PANEL_STYLE_KEY, "{}")) or {}
    except (ValueError, TypeError):
        return {}


def save_style(guild_id: int, style: dict):
    if style:
        db.set_setting(guild_id, PANEL_STYLE_KEY, json.dumps(style))
    else:
        db.delete_setting(guild_id, PANEL_STYLE_KEY)


def parse_color(text: str) -> str | None:
    text = text.strip().lstrip("#")
    return text.upper() if re.fullmatch(r"[0-9a-fA-F]{6}", text) else None


def valid_url(text: str) -> bool:
    return bool(re.fullmatch(r"https?://\S+", text.strip()))


def build_panel_embed(guild_id: int) -> discord.Embed:
    """The applications panel embed, using the server's customization (see /applications customize)."""
    style, types = get_style(guild_id), get_types(guild_id)
    embed = discord.Embed(
        title=style.get("title") or DEFAULT_PANEL_TITLE,
        color=int(style["color"], 16) if style.get("color") else discord.Color.blurple().value,
    )
    text = style.get("description") or DEFAULT_PANEL_TEXT
    if style.get("show_list", True) and types:
        text += "\n\n" + "\n".join(
            f"{entry.get('emoji', '📋')} **{name}**" + (f" - {entry['description']}" if entry.get("description") else "")
            for name, entry in types.items())
    embed.description = text[:4000]
    if style.get("image"):
        embed.set_image(url=style["image"])
    if style.get("thumbnail"):
        embed.set_thumbnail(url=style["thumbnail"])
    if style.get("footer"):
        embed.set_footer(text=style["footer"])
    return embed


async def type_name_autocomplete(interaction: discord.Interaction, current: str):
    types = get_types(interaction.guild_id)
    return [
        app_commands.Choice(name=name, value=name)
        for name in types
        if current.lower() in name.lower()
    ][:25]


def parse_questions(raw: str) -> list[str]:
    """One question per line; blank lines are skipped and leading numbering ("1.", "2)") is stripped."""
    questions = []
    for line in raw.splitlines():
        line = re.sub(r"^\s*\d{1,2}[.)]\s+", "", line).strip()
        if line:
            questions.append(line)
    return questions


def find_role(guild: discord.Guild, text: str):
    """A role from a mention, an ID or a name."""
    text = text.strip()
    m = re.search(r"\d{15,22}", text)
    if m:
        return guild.get_role(int(m.group()))
    wanted = text.lstrip("@").lower()
    return discord.utils.find(lambda r: r.name.lower() == wanted, guild.roles)


def find_text_channel(guild: discord.Guild, text: str):
    text = text.strip()
    m = re.search(r"\d{15,22}", text)
    if m:
        ch = guild.get_channel(int(m.group()))
        return ch if isinstance(ch, discord.TextChannel) else None
    wanted = text.lstrip("#").lower()
    return discord.utils.find(lambda c: c.name.lower() == wanted, guild.text_channels)


def valid_emoji(text: str) -> bool:
    """A single unicode emoji or a custom <:name:id> emoji - anything else would break the panel's menu."""
    if re.fullmatch(r"<a?:\w{2,32}:\d{15,22}>", text):
        return True
    return 0 < len(text) <= 10 and not any(c.isascii() and c.isalnum() for c in text)


def role_problem(guild: discord.Guild, role: discord.Role) -> str | None:
    if role.is_default() or role.managed:
        return f"{role.mention} can't be handed out (it's @everyone or a bot/integration role)."
    if role >= guild.me.top_role:
        return f"{role.mention} is above my highest role, so I couldn't assign it. Move my role higher first."
    return None


def channel_problem(guild: discord.Guild, channel: discord.TextChannel) -> str | None:
    perms = channel.permissions_for(guild.me)
    if not (perms.view_channel and perms.send_messages and perms.embed_links):
        return f"I can't post embeds in {channel.mention} - check my permissions there."
    return None


def build_review_pages(user, app_id: int, app_type: str, entry: dict, questions: list, answers: list) -> list:
    """The review as one or more embeds. 30 questions don't fit in one message (25 fields / 6000
    characters), so the answers are split across embeds; only the first message carries the buttons."""
    emoji = entry.get("emoji", "📋")
    first = discord.Embed(title=f"{emoji} New {app_type} Application", color=discord.Color.blue())
    first.add_field(name="Applicant", value=f"{user.mention} ({user})", inline=False)
    first.add_field(name="Application ID", value=f"#{app_id}", inline=True)
    pages, current = [first], first
    size = sum(len(f.name) + len(f.value) for f in first.fields) + len(first.title) + 700  # room for a decision header
    for q, a in zip(questions, answers):
        name, value = q[:256], (a or "*no answer*")[:1024]
        cost = len(name) + len(value)
        if len(current.fields) >= 24 or size + cost > PAGE_BUDGET:
            current = discord.Embed(title="Answers (continued)", color=discord.Color.blue())
            pages.append(current)
            size = len(current.title)
        current.add_field(name=name, value=value, inline=False)
        size += cost
    if len(pages) > 1:
        for i, page in enumerate(pages, start=1):
            page.set_footer(text=f"Application #{app_id} - page {i}/{len(pages)}")
    return pages


# Applications that send an invite link when someone is accepted, matched on the application's name (letters
# only, case-insensitive: "Voice Actor", "voice-actors" ...). An application can also have its own invite set
# with /applications make|edit invite:<link>, which wins over this list.
DEFAULT_ACCEPT_INVITES = {"voiceactor": "https://discord.gg/5hTptfZ5BP"}  # DE content server
INVITE_RE = re.compile(r"https?://(?:www\.)?(?:discord\.gg|discord(?:app)?\.com/invite)/[\w-]+/?", re.I)


def invite_for(app_type: str, entry: dict) -> str | None:
    if entry.get("invite"):
        return entry["invite"]
    letters = re.sub(r"[^a-z]", "", app_type.lower())
    return next((link for key, link in DEFAULT_ACCEPT_INVITES.items() if key in letters), None)


def invite_problem(text: str | None) -> str | None:
    if text and text.strip().lower() not in OFF_WORDS and not INVITE_RE.fullmatch(text.strip()):
        return "The invite must be a Discord invite link like `https://discord.gg/abc123`."
    return None


# Applications that hand out extra roles when someone is accepted, matched on the application's name (letters
# only, case-insensitive: "Staff Team", "staff application" ...). These are given IN ADDITION to the single
# role picked with /applications make|edit role:.
DEFAULT_ACCEPT_ROLES = {"staff": [1444997607578533929, 1516526043354103908]}


def uses_dm(app_type: str, entry: dict) -> bool:
    """True if this application is answered in the applicant's DMs instead of a pop-up form. Set per
    application with /applications make|edit dm:True|False; unset, Staff applications use DMs."""
    if "dm" in entry:
        return bool(entry["dm"])
    return "staff" in re.sub(r"[^a-z]", "", app_type.lower())


def roles_for(app_type: str, entry: dict) -> list[int]:
    """Every role ID an accepted applicant of this application gets (the picked role + name-matched ones)."""
    ids = [int(entry["role_id"])] if entry.get("role_id") else []
    letters = re.sub(r"[^a-z]", "", app_type.lower())
    for key, role_ids in DEFAULT_ACCEPT_ROLES.items():
        if key in letters:
            ids.extend(role_ids)
    return list(dict.fromkeys(ids))  # no duplicates, order kept


def describe_roles(guild: discord.Guild, role_ids: list[int]) -> str:
    if not role_ids:
        return "None"
    return ", ".join(guild.get_role(r).mention if guild.get_role(r) else f"`{r}` (not found)" for r in role_ids)


async def give_roles(guild: discord.Guild, user_id: int, role_ids: list[int], reason: str) -> list[str]:
    """Gives the member every role. Returns a line for each one that couldn't be given (empty = all done)."""
    member = guild.get_member(user_id)
    if member is None:
        return ["they're no longer in the server"] if role_ids else []
    problems = []
    for role_id in role_ids:
        role = guild.get_role(role_id)
        if role is None:
            problems.append(f"`{role_id}` - the role doesn't exist")
            continue
        if role in member.roles:
            continue
        try:
            await member.add_roles(role, reason=reason)
        except discord.HTTPException:
            problems.append(f"{role.mention} - I'm missing permissions, or my role is below it")
    return problems


class InviteLinkView(discord.ui.View):
    """A 'Join the server' link button under the accepted message."""

    def __init__(self, url: str):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label="Join the DE content server", url=url, emoji="🎬"))


def slugify_name(text: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", text.lower().strip())
    return re.sub(r"-+", "-", text).strip("-")[:20] or "app"


def app_key(name: str) -> str:
    """Short stable id for an application name, used in the panel buttons' custom_id."""
    return hashlib.sha1(name.encode("utf-8")).hexdigest()[:12]


class ApplyButton(discord.ui.DynamicItem[discord.ui.Button], template=r"app_apply:(?P<key>[0-9a-f]{12})"):
    """One button per application on the panel. Dynamic, so clicks keep working after a restart and the
    panel always reflects the server's current applications."""

    def __init__(self, key: str, label: str = "Apply", emoji=None,
                 style: discord.ButtonStyle = discord.ButtonStyle.primary):
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=style, custom_id=f"app_apply:{key}"))
        self.key = key

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(match["key"])

    async def callback(self, interaction: discord.Interaction):
        cog: "Applications" = interaction.client.get_cog("Applications")
        await cog.start_application(interaction, key=self.key)


class ApplicationPanelView(discord.ui.View):
    """The buttons under the applications panel - built per server from its current applications."""

    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        style = BUTTON_STYLES.get(get_style(guild_id).get("button", "blue"), discord.ButtonStyle.primary)
        for name, entry in list(get_types(guild_id).items())[:MAX_APPLICATION_TYPES]:
            self.add_item(ApplyButton(app_key(name), label=name[:80], emoji=entry.get("emoji"), style=style))


class LegacyApplicationSelectView(discord.ui.View):
    """Panels posted before the buttons existed had a dropdown. Keep it working: it opens the same form."""

    def __init__(self):
        super().__init__(timeout=None)
        select = discord.ui.Select(custom_id="application_panel_select",
                                   options=[discord.SelectOption(label="Application", value="x")])
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        cog: "Applications" = interaction.client.get_cog("Applications")
        await cog.start_application(interaction, name=interaction.data["values"][0])


class AppSession:
    """Answers collected so far for one member's in-progress application (held in memory)."""

    def __init__(self, name: str, questions: list[str], user_id: int):
        self.name, self.questions, self.user_id = name, questions, user_id
        self.answers: list[str] = []

    @property
    def pages(self) -> int:
        return max(1, math.ceil(len(self.questions) / QUESTIONS_PER_PAGE))


class AnswerModal(discord.ui.Modal):
    """The application form. Up to 5 questions per page; longer applications continue on the next page."""

    def __init__(self, cog: "Applications", session: AppSession, page: int):
        title = session.name if session.pages == 1 else f"{session.name} - {page + 1}/{session.pages}"
        super().__init__(title=title[:45])
        self.cog, self.session, self.page = cog, session, page
        self.inputs: list[discord.ui.TextInput] = []
        for question in session.questions[page * QUESTIONS_PER_PAGE:(page + 1) * QUESTIONS_PER_PAGE]:
            field = discord.ui.TextInput(
                label=question if len(question) <= 45 else question[:44] + "…",
                placeholder=question[:100], style=discord.TextStyle.paragraph, max_length=1000, required=True)
            self.inputs.append(field)
            self.add_item(field)

    async def on_submit(self, interaction: discord.Interaction):
        session = self.session
        # keep earlier pages, replace this one (safe if the same page is submitted twice)
        session.answers = session.answers[:self.page * QUESTIONS_PER_PAGE] + [str(i.value).strip() for i in self.inputs]
        if self.page + 1 < session.pages:
            embed = discord.Embed(
                title=f"✅ Page {self.page + 1} of {session.pages} saved",
                description="Press **Continue** for the next questions. Your answers are kept for 15 minutes.",
                color=discord.Color.green())
            return await interaction.response.send_message(
                embed=embed, view=ContinueView(self.cog, session, self.page + 1), ephemeral=True)
        await self.cog.finish_application(interaction, session)


class ContinueView(discord.ui.View):
    def __init__(self, cog: "Applications", session: AppSession, next_page: int):
        super().__init__(timeout=900)
        self.cog, self.session, self.next_page = cog, session, next_page
        self.cont.label = f"Continue (page {next_page + 1}/{session.pages})"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.session.user_id

    @discord.ui.button(label="Continue", style=discord.ButtonStyle.success)
    async def cont(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AnswerModal(self.cog, self.session, self.next_page))

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(
            embed=discord.Embed(description="Application cancelled.", color=discord.Color.greyple()), view=None)


class TicketDecisionModal(discord.ui.Modal):
    """Optional reason when staff accept or deny an application from inside its ticket."""

    def __init__(self, cog: "Applications", decision: str, message: discord.Message, app_id: int):
        super().__init__(title=f"{decision} application"[:45])
        self.cog, self.decision, self.message, self.app_id = cog, decision, message, app_id
        self.reason = discord.ui.TextInput(label="Reason (optional, sent to the applicant)",
                                           style=discord.TextStyle.paragraph, max_length=500, required=False)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.decide_in_ticket(interaction, self.decision, self.message, self.app_id,
                                        str(self.reason.value).strip() or None)


class DecisionReasonModal(discord.ui.Modal):
    def __init__(self, view: "ApplicationDecisionView", decision: str):
        super().__init__(title=f"{decision} with Reason")
        self.view_ref = view
        self.decision = decision
        self.reason = discord.ui.TextInput(label="Reason", style=discord.TextStyle.paragraph, max_length=500)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        await self.view_ref.apply_decision(interaction, self.decision, str(self.reason.value).strip())


class ApplicationDecisionView(discord.ui.View):
    def __init__(self, app_id: int):
        super().__init__(timeout=None)
        self.app_id = app_id

    def _app_id(self, message: discord.Message) -> int:
        """This view is re-registered after a restart with app_id 0 (one shared instance for every
        message), so read the real ID off the review message itself."""
        if self.app_id:
            return self.app_id
        for embed in message.embeds:
            for field in embed.fields:
                if field.name == "Application ID":
                    try:
                        return int(field.value.lstrip("#"))
                    except ValueError:
                        pass
        return 0

    async def _continuation_messages(self, interaction: discord.Interaction, app_id: int) -> list:
        """The extra answer pages posted after the review message (30 questions need several)."""
        raw = db.get_setting(interaction.guild.id, f"app_pages:{app_id}")
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return []
        channel = interaction.guild.get_channel(data.get("channel", 0))
        messages = []
        for mid in data.get("messages", []):
            try:
                messages.append(await channel.fetch_message(mid))
            except (discord.HTTPException, AttributeError):
                continue
        return messages

    async def apply_decision(self, interaction: discord.Interaction, decision: str, reason: str = None):
        app_id = self._app_id(interaction.message)
        db.set_application_status(app_id, decision.lower())
        embed = interaction.message.embeds[0]
        header = f"**{decision} by {interaction.user.mention}**"
        if reason:
            header += f" with reason: {reason}"
        embed.description = header + "\n\n" + (embed.description or "")
        embed.color = discord.Color.green() if decision == "Accepted" else discord.Color.red()

        for item in self.children:
            item.disabled = True

        # If this server has an Accepted / Denied channel set, move the decided application
        # there and delete it from the review channel. Unset (or same channel as the review
        # one) -> behaves as before and just edits the message in place.
        dest = None
        if interaction.guild:
            dest_key = "application-accepted-channel" if decision == "Accepted" else "application-denied-channel"
            dest_id = gc.get(interaction.guild.id, dest_key)
            dest = interaction.guild.get_channel(dest_id) if dest_id else None
            if dest is not None and dest.id == interaction.channel_id:
                dest = None

        moved = False
        if dest is not None:
            if not interaction.response.is_done():
                await interaction.response.defer()  # acknowledge now; the move below can take a moment
            try:
                await dest.send(embed=embed)
                moved = True
            except discord.HTTPException:
                moved = False  # can't post there (permissions/deleted) - keep the original instead
            if moved:
                # Carry the rest of the answers over too, then clean up the review channel.
                for extra in await self._continuation_messages(interaction, app_id):
                    try:
                        if extra.embeds:
                            await dest.send(embed=extra.embeds[0])
                        await extra.delete()
                    except discord.HTTPException:
                        pass
                db.delete_setting(interaction.guild.id, f"app_pages:{app_id}")
                try:
                    await interaction.message.delete()
                except discord.HTTPException:
                    pass

        if not moved:
            if interaction.response.is_done():
                await interaction.message.edit(embed=embed, view=self)
            else:
                await interaction.response.edit_message(embed=embed, view=self)

        row = db.get_application(app_id)
        if row:
            user = interaction.client.get_user(row["user_id"])
            if user:
                try:
                    legacy_invite = (invite_for(row["app_type"], get_types(row["guild_id"]).get(row["app_type"], {}))
                                     if decision == "Accepted" else None)
                    dm = discord.Embed(
                        title=f"Application {decision}",
                        description=f"Your **{row['app_type']}** application was **{decision.lower()}**."
                                     + (f"\n\n**Reason:** {reason}" if reason else "")
                                     + (f"\n\n🎬 Welcome! Join the DE content server: {legacy_invite}" if legacy_invite else ""),
                        color=embed.color,
                    )
                    await user.send(embed=dm, view=InviteLinkView(legacy_invite) if legacy_invite else discord.utils.MISSING)
                except discord.HTTPException:
                    pass

            if decision == "Accepted" and interaction.guild:
                entry = get_types(row["guild_id"]).get(row["app_type"], {})
                problems = await give_roles(interaction.guild, row["user_id"], roles_for(row["app_type"], entry),
                                            f"Application '{row['app_type']}' accepted")
                if problems:
                    try:
                        await interaction.followup.send(
                            f"⚠️ Accepted, but I couldn't give <@{row['user_id']}> every role:\n" + "\n".join(problems),
                            ephemeral=True)
                    except discord.HTTPException:
                        pass

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, custom_id="app_accept")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        await self.apply_decision(interaction, "Accepted")

    @discord.ui.button(label="Accept w/ Reason", style=discord.ButtonStyle.success, custom_id="app_accept_reason")
    async def accept_reason(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        await interaction.response.send_modal(DecisionReasonModal(self, "Accepted"))

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, custom_id="app_deny")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        await self.apply_decision(interaction, "Denied")

    @discord.ui.button(label="Deny w/ Reason", style=discord.ButtonStyle.danger, custom_id="app_deny_reason")
    async def deny_reason(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        await interaction.response.send_modal(DecisionReasonModal(self, "Denied"))

    @discord.ui.button(label="Open Ticket", style=discord.ButtonStyle.primary, emoji="🎫", custom_id="app_ticket")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        row = db.get_application(self._app_id(interaction.message))
        if not row:
            return await interaction.response.send_message("Couldn't find that application.", ephemeral=True)
        member = interaction.guild.get_member(row["user_id"])
        if not member:
            return await interaction.response.send_message("That user isn't in the server anymore.", ephemeral=True)

        tickets_cog = interaction.client.get_cog("Tickets")
        if not tickets_cog:
            return await interaction.response.send_message("Ticket system isn't loaded.", ephemeral=True)
        existing = tickets_cog.open_ticket_channel(interaction.guild, member.id)
        if existing is not None:
            return await interaction.response.send_message(
                f"{member.mention} already has an open ticket: {existing.mention}.", ephemeral=True)
        channel = await tickets_cog.create_ticket_channel(
            interaction.guild, member,
            name_override=f"{member.name}-application-ticket",
        )
        if channel is None:
            return await interaction.response.send_message("Couldn't create the ticket channel.", ephemeral=True)
        await interaction.response.send_message(f"Ticket opened: {channel.mention}", ephemeral=True)


class ConfirmDeleteView(discord.ui.View):
    def __init__(self, user_id: int, name: str):
        super().__init__(timeout=120)
        self.user_id = user_id
        self.name = name

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        types = get_types(interaction.guild_id)
        if types.pop(self.name, None) is None:
            return await interaction.response.edit_message(content="That application is already gone.", embed=None, view=None)
        save_types(interaction.guild_id, types)
        updated = await interaction.client.get_cog("Applications").refresh_panels(interaction.guild)
        await interaction.response.edit_message(
            embed=discord.Embed(description=f"🗑️ Deleted **{self.name}**." + (f" Updated {updated} posted panel(s)." if updated else ""),
                                color=discord.Color.green()), view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=discord.Embed(description="Cancelled.", color=discord.Color.greyple()), view=None)


class ApplicationModal(discord.ui.Modal):
    """The form for /applications make and /applications edit: name, emoji, description and up to 20
    questions (one per line). The accept-role and review channel come from the slash command's own
    pickers, so they're real role/channel selections rather than typed text."""

    def __init__(self, cog: "Applications", old_name: str = None, entry: dict = None, role: discord.Role = None,
                 channel: discord.TextChannel = None, clear_role: bool = False, clear_channel: bool = False,
                 invite: str = None, dm: bool = None):
        super().__init__(title=(f"Edit {old_name}" if old_name else "New application")[:45])
        self.dm = dm
        self.cog = cog
        self.old_name = old_name
        self.role, self.channel = role, channel
        self.clear_role, self.clear_channel = clear_role, clear_channel
        self.invite = invite
        entry = entry or {}

        self.name_in = discord.ui.TextInput(label="Application name", max_length=100, default=old_name,
                                            placeholder="e.g. Staff Team")
        self.emoji_in = discord.ui.TextInput(label="Emoji (optional)", max_length=40, required=False,
                                             default=entry.get("emoji") if old_name else None,
                                             placeholder="📋  - one emoji, shown next to the name")
        self.desc_in = discord.ui.TextInput(label="Description (optional)", max_length=MAX_DESCRIPTION, required=False,
                                            default=entry.get("description"),
                                            placeholder="One line shown under the name on the panel")
        self.questions_in = discord.ui.TextInput(
            label=f"Questions - one per line (max {MAX_QUESTIONS})", style=discord.TextStyle.paragraph,
            max_length=4000, default="\n".join(entry.get("questions", [])) or None,
            placeholder="How old are you?\nWhat is your timezone?\nWhy do you want to join?")
        for item in (self.name_in, self.emoji_in, self.desc_in, self.questions_in):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild

        async def fail(text: str):
            await interaction.response.send_message(
                embed=discord.Embed(title="❌ Nothing was saved", description=text, color=discord.Color.red()),
                ephemeral=True)

        name = str(self.name_in.value).strip()
        if not name or name == "__none__":
            return await fail("The application needs a name.")

        emoji = str(self.emoji_in.value).strip() or "📋"
        if not valid_emoji(emoji):
            return await fail(f"`{emoji}` isn't an emoji I can use - give one emoji (or a custom `<:name:id>` one), or leave it blank.")
        description = str(self.desc_in.value).strip()

        questions = parse_questions(str(self.questions_in.value))
        if not questions:
            return await fail("Add at least one question (one per line).")
        if len(questions) > MAX_QUESTIONS:
            return await fail(f"That's {len(questions)} questions - the maximum is {MAX_QUESTIONS}.")
        previous = get_types(guild.id).get(self.old_name, {}) if self.old_name else {}
        dm_mode = self.dm if self.dm is not None else uses_dm(name, previous)
        limit = MAX_DM_QUESTION_LENGTH if dm_mode else MAX_QUESTION_LENGTH
        for i, q in enumerate(questions, start=1):
            if len(q) > limit:
                return await fail(f"Question {i} is {len(q)} characters - keep each under {limit}"
                                  + ("." if dm_mode else " so it fits the form (or use `dm:True` to ask in DMs)."))

        types = get_types(guild.id)
        others = {n.lower() for n in types if n != self.old_name}
        if name.lower() in others:
            return await fail(f"There's already an application called **{name}**.")
        if self.old_name is None:
            if len(types) >= MAX_APPLICATION_TYPES:
                return await fail(f"This server already has the max of {MAX_APPLICATION_TYPES} applications.")
            entry = {}
        else:
            entry = types.get(self.old_name)
            if entry is None:
                return await fail("That application was deleted while the form was open.")

        entry["emoji"] = emoji
        entry["questions"] = questions
        if description:
            entry["description"] = description
        else:
            entry.pop("description", None)
        if self.role is not None:
            entry["role_id"] = self.role.id
        elif self.clear_role:
            entry.pop("role_id", None)
        if self.channel is not None:
            entry["log_channel_id"] = self.channel.id
        elif self.clear_channel:
            entry.pop("log_channel_id", None)
        if self.dm is not None:
            entry["dm"] = self.dm
        if self.invite:
            if self.invite.strip().lower() in OFF_WORDS:
                entry.pop("invite", None)
            else:
                entry["invite"] = self.invite.strip()

        if self.old_name is None:
            types[name] = entry
        elif name != self.old_name:
            types = {(name if n == self.old_name else n): (entry if n == self.old_name else e) for n, e in types.items()}
            conn = db.get_conn()  # keep existing submissions pointing at the renamed application
            conn.execute("UPDATE applications SET app_type=? WHERE guild_id=? AND app_type=?",
                         (name, guild.id, self.old_name))
            conn.commit()
            conn.close()
        save_types(guild.id, types)

        channel = guild.get_channel(entry.get("log_channel_id", 0))
        embed = discord.Embed(
            title=f"{emoji} {name} - {'updated' if self.old_name else 'created'}",
            description=description or None, color=discord.Color.green())
        embed.add_field(name="Questions", value=str(len(questions)), inline=True)
        embed.add_field(name="Roles on accept", value=describe_roles(guild, roles_for(name, entry)), inline=False)
        embed.add_field(name="Answered in", value="Direct messages" if uses_dm(name, entry) else "A pop-up form", inline=True)
        invite = invite_for(name, entry)
        if invite:
            embed.add_field(name="Invite on accept", value=invite, inline=False)
        embed.add_field(name="Review channel", value=channel.mention if channel else "Server default", inline=True)
        updated = await self.cog.refresh_panels(guild)
        embed.set_footer(text=f"Updated {updated} posted panel(s)." if updated else "Run /applications panel to post the menu.")
        await interaction.response.send_message(embed=embed, ephemeral=True)


class StyleModal(discord.ui.Modal, title="Customize the applications panel"):
    """Title, text, colour, banner image and the button colour. Footer, thumbnail and whether the
    list of applications is shown are options on the slash command."""

    def __init__(self, cog: "Applications", style: dict, footer: str = None, thumbnail: str = None, show_list: bool = None):
        super().__init__()
        self.cog, self.footer, self.thumbnail, self.show_list = cog, footer, thumbnail, show_list
        self.title_in = discord.ui.TextInput(label="Title", max_length=256, required=False,
                                             default=style.get("title") or DEFAULT_PANEL_TITLE)
        self.desc_in = discord.ui.TextInput(label="Text above the list", style=discord.TextStyle.paragraph,
                                            max_length=1500, required=False,
                                            default=style.get("description") or DEFAULT_PANEL_TEXT)
        self.color_in = discord.ui.TextInput(label="Colour (hex, e.g. 5865F2)", max_length=7, required=False,
                                             default=style.get("color"), placeholder="blank = default blurple")
        self.image_in = discord.ui.TextInput(label="Banner image URL (optional)", max_length=400, required=False,
                                             default=style.get("image"), placeholder="https://...")
        self.button_in = discord.ui.TextInput(label="Button colour (blue, grey, green or red)", max_length=6,
                                              required=False, default=style.get("button") or "blue")
        for item in (self.title_in, self.desc_in, self.color_in, self.image_in, self.button_in):
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
        for label, value in (("thumbnail", self.thumbnail),):
            if value and value.lower() not in OFF_WORDS and not valid_url(value):
                return await fail(f"The {label} must be a link starting with `http://` or `https://`.")

        def put(key, value):
            if value:
                style[key] = value
            else:
                style.pop(key, None)

        button = str(self.button_in.value).strip().lower() or "blue"
        if button not in BUTTON_STYLES:
            return await fail("The button colour must be blue, grey, green or red.")
        title, desc = str(self.title_in.value).strip(), str(self.desc_in.value).strip()
        put("title", title if title != DEFAULT_PANEL_TITLE else "")
        put("description", desc if desc != DEFAULT_PANEL_TEXT else "")
        put("color", parse_color(color_text) if color_text else "")
        put("image", image)
        put("button", button if button != "blue" else "")
        if self.footer is not None:
            put("footer", "" if self.footer.lower() in OFF_WORDS else self.footer)
        if self.thumbnail is not None:
            put("thumbnail", "" if self.thumbnail.lower() in OFF_WORDS else self.thumbnail)
        if self.show_list is not None:
            style["show_list"] = self.show_list
        save_style(guild.id, style)

        updated = await self.cog.refresh_panels(guild)
        preview = build_panel_embed(guild.id)
        note = (f"✅ Saved. Updated {updated} posted panel(s)." if updated
                else "✅ Saved. Post the panel with `/applications panel` to use it.")
        await interaction.response.send_message(content=f"{note} Preview:", embed=preview, ephemeral=True)


class Applications(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._panels_refreshed = False
        self._tasks: set[asyncio.Task] = set()  # running DM applications (kept so they aren't garbage collected)
        self.bot.add_dynamic_items(ApplyButton)          # the panel's apply buttons
        self.bot.add_view(LegacyApplicationSelectView())  # panels posted before the buttons existed
        self.bot.add_view(ApplicationDecisionView(0))
        self.active: set[int] = set()   # members mid DM-application - modmail ignores their DMs (they're answers)

    @commands.Cog.listener()
    async def on_ready(self):
        """Once per start: re-render the panels this bot posted, so they match the current settings."""
        if self._panels_refreshed:
            return
        self._panels_refreshed = True
        for guild in self.bot.guilds:
            try:
                self.apply_staff_preset(guild.id)
                await self.refresh_panels(guild)
            except Exception:
                logging.exception("couldn't refresh panels for guild %s", guild.id)

    async def post_panel(self, interaction: discord.Interaction):
        """Posted by /applications panel."""
        if not is_staff(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        if not get_types(interaction.guild_id):
            return await interaction.response.send_message(
                "No application types are configured for this server yet — use `/applications make` to create one first.",
                ephemeral=True,
            )
        message = await interaction.channel.send(embed=build_panel_embed(interaction.guild_id),
                                                 view=ApplicationPanelView(interaction.guild_id))
        # remember it so /applications customize (and make/edit/delete) can keep it up to date
        try:
            refs = json.loads(db.get_setting(interaction.guild_id, PANEL_MSGS_KEY, "[]"))
        except (ValueError, TypeError):
            refs = []
        refs.append([message.channel.id, message.id])
        db.set_setting(interaction.guild_id, PANEL_MSGS_KEY, json.dumps(refs[-10:]))
        await interaction.response.send_message("Application panel posted.", ephemeral=True)

    @staticmethod
    def apply_staff_preset(guild_id: int):
        """One time per server: make the staff application use the five staff questions and DMs. Finds the
        application with 'staff' in its name (or creates 'Staff Team'); after this it's yours to edit."""
        if db.get_setting(guild_id, "staff_app_preset_v1"):
            return
        types = get_types(guild_id)
        name = next((n for n in types if "staff" in re.sub(r"[^a-z]", "", n.lower())), None)
        if name is None:
            if len(types) >= MAX_APPLICATION_TYPES:
                return
            name = "Staff Team"
            types[name] = copy.deepcopy(DEFAULT_APPLICATIONS["Staff Team"])
        else:
            types[name]["questions"] = list(STAFF_QUESTIONS)
            types[name]["dm"] = True
        save_types(guild_id, types)
        db.set_setting(guild_id, "staff_app_preset_v1", "1")

    async def refresh_panels(self, guild: discord.Guild) -> int:
        """Re-renders every applications panel this bot posted, so edits show up without re-posting.
        Returns how many were updated; panels whose message is gone are forgotten."""
        try:
            refs = json.loads(db.get_setting(guild.id, PANEL_MSGS_KEY, "[]"))
        except (ValueError, TypeError):
            return 0
        kept, updated = [], 0
        for channel_id, message_id in refs:
            channel = guild.get_channel(channel_id)
            if channel is None:
                continue
            try:
                message = await channel.fetch_message(message_id)
                await message.edit(embed=build_panel_embed(guild.id), view=ApplicationPanelView(guild.id))
                kept.append([channel_id, message_id])
                updated += 1
            except discord.NotFound:
                continue
            except discord.HTTPException:
                kept.append([channel_id, message_id])
        db.set_setting(guild.id, PANEL_MSGS_KEY, json.dumps(kept))
        return updated

    async def start_application(self, interaction: discord.Interaction, key: str = None, name: str = None):
        """A member pressed an application button: check they can apply, then open the form - or DM them the questions."""
        guild = interaction.guild
        types = get_types(guild.id)
        if name is None:
            name = next((n for n in types if app_key(n) == key), None)
        entry = types.get(name)
        if entry is None:
            return await interaction.response.send_message(
                "That application isn't available anymore - ask staff to refresh the panel.", ephemeral=True)
        questions = entry.get("questions") or []
        if not questions:
            return await interaction.response.send_message("That application has no questions set up yet.", ephemeral=True)
        tickets = interaction.client.get_cog("Tickets")
        if tickets is None:
            return await interaction.response.send_message("The ticket system isn't loaded right now.", ephemeral=True)
        existing = tickets.open_ticket_channel(guild, interaction.user.id)
        if existing is not None:  # one ticket at a time per member
            return await interaction.response.send_message(
                f"You already have an open ticket: {existing.mention}. Please finish or close it before applying.", ephemeral=True)

        if not uses_dm(name, entry):
            return await interaction.response.send_modal(AnswerModal(self, AppSession(name, questions, interaction.user.id), 0))

        # DM application: the questions are asked one at a time in the applicant's DMs
        user = interaction.user
        if user.id in self.active:
            return await interaction.response.send_message(
                "You're already filling in an application - check your DMs!", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        try:
            dm = await user.create_dm()
            await dm.send(embed=discord.Embed(
                title=f"{entry.get('emoji', '📋')} {name} application",
                description=f"There are **{len(questions)}** questions. Reply to each message with your answer - take your time "
                            "and be detailed.\n\nType `cancel` at any point to stop.",
                color=discord.Color.blurple()))
        except discord.HTTPException:
            return await interaction.followup.send(
                "❌ I couldn't DM you. Turn on **Direct Messages** from server members (Server Settings → Privacy) "
                "and press the button again.", ephemeral=True)
        self.active.add(user.id)
        task = asyncio.create_task(self.run_dm_questions(guild, user, name, questions))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        await interaction.followup.send("📨 Check your DMs - I've sent you the questions!", ephemeral=True)

    async def run_dm_questions(self, guild: discord.Guild, user: discord.User, name: str, questions: list[str]):
        """Asks each question in DMs and waits for the reply, then submits the application."""
        try:
            dm = user.dm_channel or await user.create_dm()
            answers = []
            for i, question in enumerate(questions, start=1):
                await dm.send(embed=discord.Embed(
                    title=f"Question {i} of {len(questions)}", description=question, color=discord.Color.blurple())
                    .set_footer(text="Reply with your answer · type cancel to stop"))
                while True:
                    try:
                        msg = await self.bot.wait_for(
                            "message", timeout=900, check=lambda m: m.author.id == user.id and m.channel.id == dm.id)
                    except asyncio.TimeoutError:
                        return await dm.send("⏰ This application timed out. Press the button on the panel to start again.")
                    text = msg.content.strip()
                    if text.lower() == "cancel":
                        return await dm.send("Application cancelled.")
                    answer = text + ("\n" + "\n".join(a.url for a in msg.attachments) if msg.attachments else "")
                    if answer.strip():
                        break
                    await dm.send("Please send your answer as a message.")
                answers.append(answer[:1000])

            session = AppSession(name, questions, user.id)
            session.answers = answers
            member = guild.get_member(user.id)
            if member is None:
                return await dm.send(embed=discord.Embed(
                    title="❌ Couldn't submit", description="You're no longer in the server.", color=discord.Color.red()))
            channel, problem = await self.submit_application(guild, member, session)
            if problem:
                return await dm.send(embed=discord.Embed(title="❌ Couldn't submit", description=problem, color=discord.Color.red()))
            await dm.send(embed=discord.Embed(
                title="✅ Application submitted",
                description=f"Your **{name}** application was sent. A ticket has been opened with the staff team: {channel.mention}",
                color=discord.Color.green()))
        except discord.HTTPException:
            logging.exception("DM application for %s failed", user.id)
        finally:
            self.active.discard(user.id)

    async def submit_application(self, guild: discord.Guild, user: discord.Member, session: AppSession):
        """Saves the application and opens its ticket. Returns (channel, None) or (None, reason)."""
        entry = get_types(guild.id).get(session.name)
        if entry is None:
            return None, "That application was removed while you were filling it in."
        tickets = self.bot.get_cog("Tickets")
        if tickets is None:
            return None, "The ticket system isn't loaded right now."
        existing = tickets.open_ticket_channel(guild, user.id)
        if existing is not None:
            return None, f"You already have an open ticket: {existing.mention}."
        app_id = db.create_application(guild.id, user.id, session.name, session.answers)
        pages = build_review_pages(user, app_id, session.name, entry, session.questions, session.answers)
        channel = await tickets.create_ticket_channel(
            guild, user, name_override=f"{slugify_name(session.name)}-{slugify_name(user.name)}",
            ticket_type="application", opening_embeds=pages, application=True,
            topic=f"Application | {session.name} | {user} ({user.id})")
        if channel is None:
            return None, "I couldn't create the ticket channel - please tell an admin (I need Manage Channels)."
        # tell staff in the review channel, if one is set (per-application, else the server default)
        notify_id = entry.get("log_channel_id") or gc.get(guild.id, "application-log-channel")
        notify = self.bot.get_channel(notify_id) if notify_id else None
        if notify is not None:
            try:
                await notify.send(embed=discord.Embed(
                    title=f"{entry.get('emoji', '📋')} New {session.name} application",
                    description=f"{user.mention} applied - review it in {channel.mention}.", color=discord.Color.blurple()),
                    allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass
        return channel, None

    async def finish_application(self, interaction: discord.Interaction, session: AppSession):
        """The form's last page was submitted: save the application and open its ticket."""
        await interaction.response.defer(ephemeral=True)
        channel, problem = await self.submit_application(interaction.guild, interaction.user, session)
        if problem:
            return await interaction.followup.send(
                embed=discord.Embed(title="❌ Couldn't submit", description=problem, color=discord.Color.red()), ephemeral=True)
        await interaction.followup.send(embed=discord.Embed(
            title="✅ Application submitted",
            description=f"Your **{session.name}** application was sent. A ticket has been opened with the staff team: {channel.mention}",
            color=discord.Color.green()), ephemeral=True)

    async def prompt_decision(self, interaction: discord.Interaction, decision: str):
        """Accept / Deny pressed inside an application ticket: check, then ask for an optional reason."""
        if not is_staff(interaction.user):
            return await interaction.response.send_message("Only staff can decide applications.", ephemeral=True)
        app_id = 0
        for embed in interaction.message.embeds:
            for field in embed.fields:
                if field.name == "Application ID":
                    try:
                        app_id = int(field.value.lstrip("#"))
                    except ValueError:
                        pass
        row = db.get_application(app_id) if app_id else None
        if row is None:
            return await interaction.response.send_message("I couldn't find this application in my records.", ephemeral=True)
        if row["status"] != "pending":
            return await interaction.response.send_message(f"This application was already **{row['status']}**.", ephemeral=True)
        await interaction.response.send_modal(TicketDecisionModal(self, decision, interaction.message, app_id))

    async def decide_in_ticket(self, interaction: discord.Interaction, decision: str, message: discord.Message,
                               app_id: int, reason: str | None):
        from cogs.tickets import TicketControlView  # lazy: tickets.py imports this module
        row = db.get_application(app_id)
        if row is None or row["status"] != "pending":
            return await interaction.response.send_message("This application was already decided.", ephemeral=True)
        guild = interaction.guild
        accepted = decision == "Accepted"
        db.set_application_status(app_id, decision.lower())
        color = discord.Color.green() if accepted else discord.Color.red()

        # update the opening message: show the decision and drop the Accept / Deny buttons
        header = f"**{decision} by {interaction.user.mention}**" + (f" - {reason}" if reason else "")
        embed = discord.Embed.from_dict(message.embeds[0].to_dict())
        embed.description = header + "\n\n" + (embed.description or "")
        embed.color = color
        try:
            await message.edit(embed=embed, view=TicketControlView())
        except discord.HTTPException:
            pass
        invite = invite_for(row["app_type"], get_types(guild.id).get(row["app_type"], {})) if accepted else None
        await interaction.response.send_message(
            content=f"<@{row['user_id']}>",
            embed=discord.Embed(title=f"{'✅' if accepted else '❌'} Application {decision.lower()}",
                                description=f"Decided by {interaction.user.mention}." + (f"\n**Reason:** {reason}" if reason else "")
                                            + (f"\n\n🎬 Welcome! Join the DE content server: {invite}" if invite else ""),
                                color=color),
            view=InviteLinkView(invite) if invite else discord.utils.MISSING)

        # DM the applicant (best effort)
        user = guild.get_member(row["user_id"]) or interaction.client.get_user(row["user_id"])
        if user is not None:
            try:
                await user.send(embed=discord.Embed(
                    title=f"Application {decision}",
                    description=f"Your **{row['app_type']}** application in **{guild.name}** was **{decision.lower()}**."
                                + (f"\n\n**Reason:** {reason}" if reason else "")
                                + (f"\n\n🎬 Welcome! Join the DE content server: {invite}" if invite else ""), color=color),
                    view=InviteLinkView(invite) if invite else discord.utils.MISSING)
            except discord.HTTPException:
                pass

        entry = get_types(guild.id).get(row["app_type"], {})
        # roles on accept (the application's picked role + any name-matched ones, e.g. the staff roles)
        if accepted:
            problems = await give_roles(guild, row["user_id"], roles_for(row["app_type"], entry),
                                        f"Application '{row['app_type']}' accepted")
            if problems:
                await interaction.followup.send(
                    f"⚠️ Accepted, but I couldn't give <@{row['user_id']}> every role:\n" + "\n".join(problems), ephemeral=True)

        # keep a copy in the accepted / denied log channel, if set - the ticket will be deleted eventually
        dest_id = gc.get(guild.id, "application-accepted-channel" if accepted else "application-denied-channel")
        dest = guild.get_channel(dest_id) if dest_id else None
        if dest is not None:
            try:
                answers = json.loads(row["answers"])
                questions = entry.get("questions", [])
                if len(questions) != len(answers):
                    questions = [f"Question {i}" for i in range(1, len(answers) + 1)]
                applicant = user or await interaction.client.fetch_user(row["user_id"])
                pages = build_review_pages(applicant, app_id, row["app_type"], entry, questions, answers)
                pages[0].title = f"{'✅ Accepted' if accepted else '❌ Denied'}: {row['app_type']}"
                pages[0].description = header + (f"\nTicket: {interaction.channel.mention}" if interaction.channel else "")
                pages[0].color = color
                for page in pages:
                    await dest.send(embed=page)
            except (discord.HTTPException, ValueError):
                pass

    # ---------------- /applications make | edit | delete | list | panel ----------------

    applications_group = app_commands.Group(name="applications", description="[Staff] Create, edit and post application forms")

    @applications_group.command(name="make", description="[Admin] Create an application (opens a form)")
    @app_commands.describe(role="Role given when someone is accepted (optional)",
                           channel="Where staff are told about new applications (default: the server's application log)",
                           invite="A Discord invite link sent to people when they're accepted (optional)",
                           dm="Ask the questions in the applicant's DMs instead of a pop-up form")
    async def app_make(self, interaction: discord.Interaction, role: discord.Role = None,
                       channel: discord.TextChannel = None, invite: str = None, dm: bool = None):
        if not is_admin_perm(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        if len(get_types(interaction.guild_id)) >= MAX_APPLICATION_TYPES:
            return await interaction.response.send_message(
                f"This server already has the max of {MAX_APPLICATION_TYPES} applications - edit or delete one.", ephemeral=True)
        problem = (role_problem(interaction.guild, role) if role else None) or \
                  (channel_problem(interaction.guild, channel) if channel else None) or invite_problem(invite)
        if problem:
            return await interaction.response.send_message(problem, ephemeral=True)
        await interaction.response.send_modal(ApplicationModal(self, role=role, channel=channel, invite=invite, dm=dm))

    @applications_group.command(name="edit", description="[Admin] Edit an application (opens a form)")
    @app_commands.describe(name="The application to edit",
                           role="New role given when someone is accepted",
                           channel="New review channel",
                           invite="Invite link sent when someone is accepted (use 'off' to remove)",
                           dm="Ask the questions in DMs (True) or a pop-up form (False)",
                           clear_role="Stop giving a role on accept",
                           clear_channel="Go back to the server's default review channel")
    @app_commands.autocomplete(name=type_name_autocomplete)
    async def app_edit(self, interaction: discord.Interaction, name: str, role: discord.Role = None,
                       channel: discord.TextChannel = None, invite: str = None, dm: bool = None,
                       clear_role: bool = False, clear_channel: bool = False):
        if not is_admin_perm(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        types = get_types(interaction.guild_id)
        if name not in types:
            return await interaction.response.send_message(f"No application called **{name}**.", ephemeral=True)
        problem = (role_problem(interaction.guild, role) if role else None) or \
                  (channel_problem(interaction.guild, channel) if channel else None) or invite_problem(invite)
        if problem:
            return await interaction.response.send_message(problem, ephemeral=True)
        entry = types[name]
        if len("\n".join(entry.get("questions", []))) > 4000:
            return await interaction.response.send_message(
                "That application's questions are too long to edit in one form.", ephemeral=True)
        await interaction.response.send_modal(
            ApplicationModal(self, name, entry, role=role, channel=channel, clear_role=clear_role, clear_channel=clear_channel,
                             invite=invite, dm=dm))

    @applications_group.command(name="delete", description="[Admin] Delete an application")
    @app_commands.describe(name="The application to delete")
    @app_commands.autocomplete(name=type_name_autocomplete)
    async def app_delete(self, interaction: discord.Interaction, name: str):
        if not is_admin_perm(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        if name not in get_types(interaction.guild_id):
            return await interaction.response.send_message(f"No application called **{name}**.", ephemeral=True)
        await interaction.response.send_message(
            embed=discord.Embed(description=f"Delete the **{name}** application? This can't be undone.",
                                color=discord.Color.red()),
            view=ConfirmDeleteView(interaction.user.id, name), ephemeral=True)

    @applications_group.command(name="list", description="[Staff] Show every application and how it's set up")
    async def app_list(self, interaction: discord.Interaction):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        types = get_types(interaction.guild_id)
        if not types:
            return await interaction.response.send_message("No applications yet - create one with `/applications make`.", ephemeral=True)
        guild = interaction.guild
        embed = discord.Embed(title="📋 Applications", color=discord.Color.blurple())
        for name, entry in types.items():
            channel = guild.get_channel(entry.get("log_channel_id", 0))
            lines = [entry.get("description") or "*no description*",
                     f"{len(entry.get('questions', []))} question(s)",
                     f"Roles on accept: {describe_roles(guild, roles_for(name, entry))}",
                     f"Answered in: {'DMs' if uses_dm(name, entry) else 'a pop-up form'}",
                     f"Invite on accept: {invite_for(name, entry) or 'none'}",
                     f"Review channel: {channel.mention if channel else 'server default'}"]
            embed.add_field(name=f"{entry.get('emoji', '📋')} {name}", value="\n".join(lines)[:1024], inline=False)
            if len(embed.fields) >= 25:
                break
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @applications_group.command(name="customize", description="[Admin] Change how the applications panel looks")
    @app_commands.describe(
        footer="Small text at the bottom (use 'off' to remove)",
        thumbnail="Small image link in the corner (use 'off' to remove)",
        show_list="Show the list of applications under the text (default: yes)",
        reset="Put the panel back to its default look",
    )
    async def app_customize(self, interaction: discord.Interaction, footer: str = None, thumbnail: str = None,
                            show_list: bool = None, reset: bool = False):
        if not is_admin_perm(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        if reset:
            save_style(interaction.guild_id, {})
            await interaction.response.defer(ephemeral=True)
            updated = await self.refresh_panels(interaction.guild)
            return await interaction.followup.send(
                content="♻️ The panel is back to its default look" + (f" (updated {updated} posted panel(s))." if updated else "."),
                embed=build_panel_embed(interaction.guild_id), ephemeral=True)
        await interaction.response.send_modal(
            StyleModal(self, get_style(interaction.guild_id), footer=footer, thumbnail=thumbnail, show_list=show_list))

    @applications_group.command(name="panel", description="[Staff] Post the applications menu in this channel")
    async def app_panel(self, interaction: discord.Interaction):
        await self.post_panel(interaction)


async def setup(bot: commands.Bot):
    await bot.add_cog(Applications(bot))
