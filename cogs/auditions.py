"""
Voice actor auditions.

Staff post a script with /audition post. Members press "Submit Audition", the bot DMs them, and they reply with
a recording (an audio file or a link). Every submission is posted in the review channel with these buttons:

  Like  - DMs them "Want to be a VA?" (Yes / No). Yes gives the Voice Actor role + the server invite.
  Use   - DMs them asking permission to use the recording in a video that will be posted (Yes / No).
  Pass  - DMs them a polite no (Pass w/ reason adds a note). Only possible before Like / Use has been pressed.

No tickets are opened. Everything is stored in the generic settings table (JSON values), so there's no new table.
"""
import asyncio
import datetime
import json
import logging
import re

import discord
from discord import app_commands
from discord.ext import commands

import database as db
import guild_config as gc
from cogs.applications import InviteLinkView, get_types, give_roles, invite_for, is_staff, is_voice_actor, roles_for

DM_TIMEOUT = 900  # seconds a member has to send their recording
MAX_SCRIPT = 3500
AUDIO_EXTS = (".mp3", ".wav", ".ogg", ".oga", ".opus", ".m4a", ".aac", ".flac", ".webm", ".mp4")
URL_RE = re.compile(r"https?://\S+", re.I)


# ---------------- storage ----------------

def _load(gid: int, key: str):
    raw = db.get_setting(gid, key)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def _save(gid: int, key: str, data):
    db.set_setting(gid, key, json.dumps(data))


def next_id(gid: int, name: str) -> int:
    key = f"audition_{name}_next"
    n = int(db.get_setting(gid, key, "0") or 0) + 1
    db.set_setting(gid, key, n)
    return n


def get_aud(gid: int, aid: int):
    return _load(gid, f"audition:{aid}")


def save_aud(gid: int, aud: dict):
    _save(gid, f"audition:{aud['id']}", aud)


def get_sub(gid: int, sid: int):
    return _load(gid, f"audition_sub:{sid}")


def save_sub(gid: int, sub: dict):
    _save(gid, f"audition_sub:{sub['id']}", sub)


def all_auditions(gid: int) -> list[dict]:
    out = []
    for row in db.get_settings_by_prefix(gid, "audition:"):
        try:
            out.append(json.loads(row["value"]))
        except (ValueError, TypeError):
            continue
    return sorted(out, key=lambda a: a["id"], reverse=True)


def submission_count(gid: int, aid: int) -> int:
    return len(db.get_settings_by_prefix(gid, f"audition_user:{aid}:"))


def is_open(aud: dict) -> bool:
    if aud.get("closed"):
        return False
    deadline = aud.get("deadline")
    return not deadline or datetime.datetime.now(datetime.timezone.utc).timestamp() < deadline


def parse_deadline(text: str):
    """'3 days' / '12h' / '2 weeks' / '2026-11-05'  ->  (unix timestamp or None, error or None)."""
    t = text.strip().lower()
    if not t:
        return None, None
    now = datetime.datetime.now(datetime.timezone.utc)
    m = re.fullmatch(r"(\d+)\s*(h|hr|hrs|hour|hours|d|day|days|w|week|weeks)", t)
    if m:
        n, unit = int(m.group(1)), m.group(2)[0]
        delta = {"h": datetime.timedelta(hours=n), "d": datetime.timedelta(days=n), "w": datetime.timedelta(weeks=n)}[unit]
        return (now + delta).timestamp(), None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", t):
        try:
            end = datetime.datetime.strptime(t, "%Y-%m-%d").replace(hour=23, minute=59, tzinfo=datetime.timezone.utc)
        except ValueError:
            return None, "That date doesn't exist."
        if end <= now:
            return None, "The deadline has to be in the future."
        return end.timestamp(), None
    return None, "Use a deadline like `3 days`, `12h`, `2 weeks` or `2026-11-05` (or leave it empty)."


def audio_attachment(msg: discord.Message):
    for a in msg.attachments:
        if (a.content_type or "").startswith("audio/") or a.filename.lower().endswith(AUDIO_EXTS):
            return a
    return None


# ---------------- embeds ----------------

def script_embed(aud: dict) -> discord.Embed:
    closed = not is_open(aud)
    embed = discord.Embed(
        title=f"{'🔒' if closed else '🎤'} Audition: {aud['title']}",
        description=aud["script"],
        color=discord.Color.dark_grey() if closed else discord.Color.blurple(),
    )
    if aud.get("deadline"):
        embed.add_field(name="Deadline", value=f"<t:{int(aud['deadline'])}:R>", inline=True)
    embed.add_field(name="Status", value="Closed" if closed else "Open - press the button below", inline=True)
    embed.set_footer(text=f"Audition #{aud['id']} · you'll send your recording in a DM")
    return embed


def script_view(aud: dict) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(SubmitButton(aud["id"], disabled=not is_open(aud)))
    return view


OFFER_TEXT = {"none": "—", "offered": "⏳ Waiting for their reply", "yes": "✅ Yes", "no": "❌ No"}


def render_review(sub: dict, aud: dict | None) -> tuple[discord.Embed, discord.ui.View]:
    passed = sub["status"] == "passed"
    color = discord.Color.red() if passed else (discord.Color.green() if sub["like"] == "yes" else discord.Color.blue())
    embed = discord.Embed(title="🎤 New audition", color=color, timestamp=datetime.datetime.fromtimestamp(
        sub["created"], datetime.timezone.utc))
    embed.add_field(name="Applicant", value=f"<@{sub['user_id']}> ({sub['user_name']})", inline=False)
    embed.add_field(name="Script", value=f"{aud['title'] if aud else 'Removed script'} (#{sub['audition_id']})", inline=True)
    embed.add_field(name="Submission", value=f"#{sub['id']}", inline=True)
    if sub.get("link"):
        embed.add_field(name="Recording", value=sub["link"][:1024], inline=False)
    else:
        embed.add_field(name="Recording", value="Attached below ⬇️", inline=False)
    embed.add_field(name="Status", value="❌ Passed" + (f" - {sub['pass_reason']}" if sub.get("pass_reason") else "")
                    if passed else "Pending review", inline=False)
    embed.add_field(name="VA offer", value=OFFER_TEXT["none" if sub["like"] == "none" else sub["like"]], inline=True)
    embed.add_field(name="Video permission", value=OFFER_TEXT[sub["use"]], inline=True)
    return embed, ReviewView(sub)


# ---------------- buttons ----------------

class SubmitButton(discord.ui.DynamicItem[discord.ui.Button], template=r"audition_submit:(?P<id>\d+)"):
    """The 'Submit Audition' button under a posted script. Dynamic, so it keeps working after a restart."""

    def __init__(self, aid: int, disabled: bool = False):
        super().__init__(discord.ui.Button(label="Submit Audition", emoji="🎤", style=discord.ButtonStyle.primary,
                                           custom_id=f"audition_submit:{aid}", disabled=disabled))
        self.aid = aid

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match):
        return cls(int(match["id"]))

    async def callback(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("Auditions")
        if cog is None:
            return await interaction.response.send_message("Auditions aren't loaded right now.", ephemeral=True)
        await cog.start_submission(interaction, self.aid)


class ReplyButton(discord.ui.DynamicItem[discord.ui.Button],
                  template=r"audition_reply:(?P<g>\d+):(?P<s>\d+):(?P<k>va|use):(?P<a>yes|no)"):
    """Yes / No under a DM from the bot (the VA offer, or the video permission request)."""

    def __init__(self, gid: int, sid: int, kind: str, answer: str):
        super().__init__(discord.ui.Button(
            label="Yes" if answer == "yes" else "No",
            style=discord.ButtonStyle.success if answer == "yes" else discord.ButtonStyle.secondary,
            custom_id=f"audition_reply:{gid}:{sid}:{kind}:{answer}"))
        self.gid, self.sid, self.kind, self.answer = gid, sid, kind, answer

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match):
        return cls(int(match["g"]), int(match["s"]), match["k"], match["a"])

    async def callback(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("Auditions")
        if cog is None:
            return await interaction.response.send_message("Auditions aren't loaded right now.", ephemeral=True)
        await cog.handle_reply(interaction, self.gid, self.sid, self.kind, self.answer)


def reply_view(gid: int, sid: int, kind: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(ReplyButton(gid, sid, kind, "yes"))
    view.add_item(ReplyButton(gid, sid, kind, "no"))
    return view


class PassReasonModal(discord.ui.Modal):
    def __init__(self, cog: "Auditions", message: discord.Message):
        super().__init__(title="Pass with a reason")
        self.cog, self.message = cog, message
        self.reason = discord.ui.TextInput(label="Reason (sent to them)", style=discord.TextStyle.paragraph,
                                           max_length=500, required=True)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.review_action(interaction, "pass", self.message, str(self.reason.value).strip())


class ReviewView(discord.ui.View):
    """Like / Use / Pass under every submission in the review channel. Registered once (no submission), the
    real submission is read off the message's 'Submission' field, so the buttons survive restarts."""

    def __init__(self, sub: dict | None = None):
        super().__init__(timeout=None)
        if sub:
            asked = sub["like"] != "none" or sub["use"] != "none"
            self.like.disabled = sub["like"] != "none" or sub["status"] == "passed"
            self.use.disabled = sub["use"] != "none" or sub["status"] == "passed"
            self.pass_.disabled = asked or sub["status"] == "passed"
            self.pass_reason.disabled = self.pass_.disabled

    @discord.ui.button(label="Like", emoji="👍", style=discord.ButtonStyle.success, custom_id="audition_like")
    async def like(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.client.get_cog("Auditions").review_action(interaction, "like", interaction.message)

    @discord.ui.button(label="Use", emoji="📹", style=discord.ButtonStyle.primary, custom_id="audition_use")
    async def use(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.client.get_cog("Auditions").review_action(interaction, "use", interaction.message)

    @discord.ui.button(label="Pass", style=discord.ButtonStyle.danger, custom_id="audition_pass")
    async def pass_(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.client.get_cog("Auditions").review_action(interaction, "pass", interaction.message)

    @discord.ui.button(label="Pass w/ reason", style=discord.ButtonStyle.danger, custom_id="audition_pass_reason")
    async def pass_reason(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("Auditions")
        if not is_staff(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        await interaction.response.send_modal(PassReasonModal(cog, interaction.message))


class AuditionModal(discord.ui.Modal, title="Post an audition script"):
    def __init__(self, cog: "Auditions", channel: discord.TextChannel):
        super().__init__()
        self.cog, self.channel = cog, channel
        self.name_input = discord.ui.TextInput(label="Title", max_length=80, placeholder="e.g. Episode 12 - Villain monologue")
        self.script = discord.ui.TextInput(label="Script", style=discord.TextStyle.paragraph, max_length=MAX_SCRIPT)
        self.deadline = discord.ui.TextInput(label="Deadline (optional)", required=False, max_length=30,
                                             placeholder="3 days, 12h, 2 weeks or 2026-11-05")
        self.add_item(self.name_input)
        self.add_item(self.script)
        self.add_item(self.deadline)

    async def on_submit(self, interaction: discord.Interaction):
        deadline, problem = parse_deadline(str(self.deadline.value))
        if problem:
            return await interaction.response.send_message(problem, ephemeral=True)
        await self.cog.post_script(interaction, self.channel, str(self.name_input.value).strip(),
                                   str(self.script.value).strip(), deadline)


# ---------------- the cog ----------------

class Auditions(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._tasks: set[asyncio.Task] = set()
        self._fallback_active: set[int] = set()
        self.bot.add_dynamic_items(SubmitButton, ReplyButton)
        self.bot.add_view(ReviewView())

    @property
    def active(self) -> set[int]:
        """Members mid-audition. Shared with the applications cog's set. Modmail (Modmail.in_dm_flow) ignores their DMs, won't open a thread for them and won't relay to one they already have open."""
        apps = self.bot.get_cog("Applications")
        return apps.active if apps is not None else self._fallback_active

    # ---- staff commands ----

    audition_group = app_commands.Group(name="audition", description="[Staff] Voice actor auditions")

    @audition_group.command(name="post", description="[Staff] Post a script for members to audition for")
    @app_commands.describe(channel="Where to post it (default: this channel)")
    async def audition_post(self, interaction: discord.Interaction, channel: discord.TextChannel = None):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        await interaction.response.send_modal(AuditionModal(self, channel or interaction.channel))

    async def post_script(self, interaction: discord.Interaction, channel, title: str, script: str, deadline):
        gid = interaction.guild_id
        aud = {"id": next_id(gid, "id"), "title": title, "script": script, "deadline": deadline, "closed": False,
               "channel_id": channel.id, "message_id": 0, "created_by": interaction.user.id}
        try:
            msg = await channel.send(embed=script_embed(aud), view=script_view(aud))
        except discord.HTTPException:
            return await interaction.response.send_message(
                f"I couldn't post in {channel.mention} - I need View Channel and Send Messages there.", ephemeral=True)
        aud["message_id"] = msg.id
        save_aud(gid, aud)
        await interaction.response.send_message(f"✅ Posted audition **#{aud['id']}**: {msg.jump_url}", ephemeral=True)

    async def _aud_autocomplete(self, interaction: discord.Interaction, current: str):
        auds = [a for a in all_auditions(interaction.guild_id) if is_open(a)]
        return [app_commands.Choice(name=f"#{a['id']} {a['title']}"[:100], value=str(a["id"]))
                for a in auds if current.lower() in a["title"].lower() or current in str(a["id"])][:25]

    @audition_group.command(name="close", description="[Staff] Stop taking submissions for a script")
    @app_commands.describe(script="The script to close")
    @app_commands.autocomplete(script=_aud_autocomplete)
    async def audition_close(self, interaction: discord.Interaction, script: str):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        gid = interaction.guild_id
        aud = get_aud(gid, int(script)) if script.isdigit() else None
        if aud is None:
            return await interaction.response.send_message("I couldn't find that script.", ephemeral=True)
        aud["closed"] = True
        save_aud(gid, aud)
        await self._refresh_script(interaction.guild, aud)
        await interaction.response.send_message(f"🔒 Closed **{aud['title']}** (#{aud['id']}).", ephemeral=True)

    @audition_group.command(name="list", description="[Staff] Show the scripts and how many submissions each has")
    async def audition_list(self, interaction: discord.Interaction):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        gid = interaction.guild_id
        auds = all_auditions(gid)
        if not auds:
            return await interaction.response.send_message("No scripts yet - post one with `/audition post`.", ephemeral=True)
        embed = discord.Embed(title="🎤 Auditions", color=discord.Color.blurple())
        for a in auds[:15]:
            deadline = f" · deadline <t:{int(a['deadline'])}:R>" if a.get("deadline") else ""
            embed.add_field(name=f"#{a['id']} {a['title']}"[:256],
                            value=f"{'🟢 Open' if is_open(a) else '🔒 Closed'} · {submission_count(gid, a['id'])} submission(s){deadline}",
                            inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _refresh_script(self, guild: discord.Guild, aud: dict):
        channel = guild.get_channel(aud.get("channel_id", 0))
        if channel is None or not aud.get("message_id"):
            return
        try:
            await channel.get_partial_message(aud["message_id"]).edit(embed=script_embed(aud), view=script_view(aud))
        except discord.HTTPException:
            pass

    # ---- member side: submitting ----

    async def start_submission(self, interaction: discord.Interaction, aid: int):
        guild = interaction.guild
        user = interaction.user
        aud = get_aud(guild.id, aid)
        if aud is None:
            return await interaction.response.send_message("That script isn't available anymore.", ephemeral=True)
        if not is_open(aud):
            return await interaction.response.send_message("Submissions for this script are closed.", ephemeral=True)
        if db.get_setting(guild.id, f"audition_user:{aid}:{user.id}"):
            return await interaction.response.send_message("You've already submitted an audition for this script.", ephemeral=True)
        if user.id in self.active:
            return await interaction.response.send_message("You're already in the middle of one - check your DMs!", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        try:
            dm = await user.create_dm()
            await dm.send(embed=discord.Embed(
                title=f"🎤 Audition: {aud['title']}",
                description=f"{aud['script']}\n\n**Reply to this message with your recording** - upload an audio file "
                            "(or a voice message), or send a link.\nType `cancel` to stop.",
                color=discord.Color.blurple()))
        except discord.HTTPException:
            return await interaction.followup.send(
                "❌ I couldn't DM you. Turn on **Direct Messages** from server members (Server Settings → Privacy) "
                "and press the button again.", ephemeral=True)
        self.active.add(user.id)
        task = asyncio.create_task(self.run_submission(guild, user, aid))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        await interaction.followup.send("📨 Check your DMs - send your recording there!", ephemeral=True)

    async def run_submission(self, guild: discord.Guild, user: discord.abc.User, aid: int):
        try:
            dm = user.dm_channel or await user.create_dm()
            while True:
                try:
                    msg = await self.bot.wait_for(
                        "message", timeout=DM_TIMEOUT, check=lambda m: m.author.id == user.id and m.channel.id == dm.id)
                except asyncio.TimeoutError:
                    return await dm.send("⏰ This audition timed out. Press the button on the script to start again.")
                if msg.content.strip().lower() == "cancel":
                    return await dm.send("Audition cancelled.")
                attachment = audio_attachment(msg)
                link = None if attachment else (URL_RE.search(msg.content).group(0) if URL_RE.search(msg.content) else None)
                if attachment or link:
                    break
                await dm.send("Please upload an **audio file** (or voice message) or send a **link** to your recording.")

            problem = await self.submit(guild, user, aid, attachment, link)
            if problem:
                return await dm.send(embed=discord.Embed(title="❌ Couldn't submit", description=problem, color=discord.Color.red()))
            await dm.send(embed=discord.Embed(
                title="✅ Audition submitted",
                description="Thanks! The staff team will listen to it. If they like it you'll get a DM here.",
                color=discord.Color.green()))
        except discord.HTTPException:
            logging.exception("Audition DM flow for %s failed", user.id)
        finally:
            self.active.discard(user.id)

    async def submit(self, guild: discord.Guild, user, aid: int, attachment, link) -> str | None:
        """Posts the submission in the review channel. Returns a problem message, or None on success."""
        aud = get_aud(guild.id, aid)
        if aud is None or not is_open(aud):
            return "Submissions for this script closed while you were recording."
        if db.get_setting(guild.id, f"audition_user:{aid}:{user.id}"):
            return "You've already submitted an audition for this script."
        channel_id = gc.get(guild.id, "audition-review-channel") or gc.get(guild.id, "application-log-channel")
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel is None:
            return "No audition review channel is set up yet - please tell an admin."

        sub = {"id": next_id(guild.id, "sub"), "audition_id": aid, "user_id": user.id, "user_name": str(user),
               "status": "pending", "like": "none", "use": "none", "link": link, "pass_reason": None,
               "created": datetime.datetime.now(datetime.timezone.utc).timestamp(),
               "review_channel": channel.id, "review_message": 0}
        file = None
        if attachment is not None:
            try:  # re-upload so the recording lives in the review channel and plays right there
                file = await attachment.to_file()
            except discord.HTTPException:
                sub["link"] = attachment.url
        embed, view = render_review(sub, aud)
        try:
            msg = await channel.send(embed=embed, view=view, file=file) if file else await channel.send(embed=embed, view=view)
        except discord.HTTPException:
            if file is None:
                return "I couldn't post your audition in the review channel - please tell an admin."
            try:  # most likely too big to re-upload: fall back to the original link
                sub["link"] = attachment.url
                embed, view = render_review(sub, aud)
                msg = await channel.send(embed=embed, view=view)
            except discord.HTTPException:
                return "I couldn't post your audition in the review channel - please tell an admin."
        sub["review_message"] = msg.id
        save_sub(guild.id, sub)
        db.set_setting(guild.id, f"audition_user:{aid}:{user.id}", sub["id"])
        return None

    # ---- staff side: Like / Use / Pass ----

    @staticmethod
    def _sub_id(message: discord.Message) -> int:
        for embed in message.embeds:
            for field in embed.fields:
                if field.name == "Submission":
                    try:
                        return int(field.value.lstrip("#"))
                    except ValueError:
                        pass
        return 0

    async def _dm(self, user_id: int, **kwargs) -> bool:
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            await user.send(**kwargs)
            return True
        except discord.HTTPException:
            return False

    async def review_action(self, interaction: discord.Interaction, action: str, message: discord.Message,
                            reason: str | None = None):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        gid = interaction.guild_id
        sub = get_sub(gid, self._sub_id(message))
        if sub is None:
            return await interaction.response.send_message("I couldn't find this submission in my records.", ephemeral=True)
        aud = get_aud(gid, sub["audition_id"])
        title = aud["title"] if aud else "the script"
        guild_name = interaction.guild.name

        if sub["status"] == "passed":
            return await interaction.response.send_message("This audition was already passed.", ephemeral=True)

        if action == "like":
            if sub["like"] != "none":
                return await interaction.response.send_message("You already sent them the VA offer.", ephemeral=True)
            sent = await self._dm(sub["user_id"], embed=discord.Embed(
                title="🎤 We liked your audition!",
                description=f"Your audition for **{title}** in **{guild_name}** stood out. "
                            "Would you like to become a voice actor (VA) with us?", color=discord.Color.green()),
                view=reply_view(gid, sub["id"], "va"))
            if not sent:
                return await interaction.response.send_message("I couldn't DM them - their DMs are closed.", ephemeral=True)
            sub["like"] = "offered"
        elif action == "use":
            if sub["use"] != "none":
                return await interaction.response.send_message("You already asked for their permission.", ephemeral=True)
            sent = await self._dm(sub["user_id"], embed=discord.Embed(
                title="📹 Can we use your recording?",
                description=f"We'd love to use your audition for **{title}** in **{guild_name}** in a video that will be "
                            "posted. Do we have your permission?", color=discord.Color.blurple()),
                view=reply_view(gid, sub["id"], "use"))
            if not sent:
                return await interaction.response.send_message("I couldn't DM them - their DMs are closed.", ephemeral=True)
            sub["use"] = "asked"
        elif action == "pass":
            if sub["like"] != "none" or sub["use"] != "none":
                return await interaction.response.send_message(
                    "You've already messaged them about this one, so it can't be passed anymore.", ephemeral=True)
            await self._dm(sub["user_id"], embed=discord.Embed(
                title="Thanks for auditioning",
                description=f"We listened to your audition for **{title}** in **{guild_name}** and won't be moving "
                            "forward this time. Please do try again on the next script!"
                            + (f"\n\n**Note from the team:** {reason}" if reason else ""),
                color=discord.Color.red()))
            sub["status"] = "passed"
            sub["pass_reason"] = reason
        save_sub(gid, sub)

        # "offered" / "asked" are shown as waiting; map to the display states
        shown = dict(sub)
        shown["like"] = {"none": "none", "offered": "offered"}.get(sub["like"], sub["like"])
        shown["use"] = {"none": "none", "asked": "offered"}.get(sub["use"], sub["use"])
        embed, view = render_review(shown, aud)
        if interaction.response.is_done():
            await message.edit(embed=embed, view=view)
        else:
            await interaction.response.edit_message(embed=embed, view=view)

    # ---- member side: answering the DMs ----

    async def handle_reply(self, interaction: discord.Interaction, gid: int, sid: int, kind: str, answer: str):
        sub = get_sub(gid, sid)
        guild = self.bot.get_guild(gid)
        if sub is None or guild is None or interaction.user.id != sub["user_id"]:
            return await interaction.response.send_message("This isn't available anymore.", ephemeral=True)
        field = "like" if kind == "va" else "use"
        waiting = "offered" if kind == "va" else "asked"
        if sub[field] != waiting:
            return await interaction.response.edit_message(view=None)  # already answered - just clear the buttons
        sub[field] = answer
        save_sub(gid, sub)
        aud = get_aud(gid, sub["audition_id"])
        title = aud["title"] if aud else "the script"

        invite, problems = None, []
        if kind == "va" and answer == "yes":
            name = next((n for n in get_types(gid) if is_voice_actor(n)), None)
            entry = get_types(gid).get(name, {}) if name else {}
            invite = invite_for(name, entry) if name else None
            if name:
                problems = await give_roles(guild, sub["user_id"], roles_for(name, entry), "Accepted the VA offer after an audition")
            text = ("🎉 Welcome to the team! You've been given the Voice Actor role in **" + guild.name + "**."
                    + (f"\n\n🎬 Join the DE content server: {invite}" if invite else ""))
            result = discord.Embed(title="Welcome aboard!", description=text, color=discord.Color.green())
        elif kind == "va":
            result = discord.Embed(title="No worries!", description="Thanks for auditioning - the door's always open.",
                                   color=discord.Color.greyple())
        elif answer == "yes":
            result = discord.Embed(title="Thank you!", description="You've given us permission to use your recording "
                                   f"for **{title}** in a video that will be posted.", color=discord.Color.green())
        else:
            result = discord.Embed(title="No problem", description="We won't use your recording.", color=discord.Color.greyple())
        await interaction.response.edit_message(embed=result, view=InviteLinkView(invite) if invite else None)

        # update the review message and tell staff
        shown = dict(sub)
        shown["use"] = "offered" if sub["use"] == "asked" else sub["use"]
        channel = guild.get_channel(sub["review_channel"])
        if channel is not None:
            embed, view = render_review(shown, aud)
            try:
                await channel.get_partial_message(sub["review_message"]).edit(embed=embed, view=view)
            except discord.HTTPException:
                pass
            what = ("accepted the VA offer" if answer == "yes" else "declined the VA offer") if kind == "va" else \
                   ("gave permission to use their recording" if answer == "yes" else "did **not** give permission to use their recording")
            note = f"{'✅' if answer == 'yes' else '❌'} <@{sub['user_id']}> {what} (audition #{sub['id']})."
            if problems:
                note += "\n⚠️ I couldn't give them every role:\n" + "\n".join(problems)
            try:
                await channel.send(note, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass


async def setup(bot: commands.Bot):
    await bot.add_cog(Auditions(bot))
