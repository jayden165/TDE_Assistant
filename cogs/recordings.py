"""
Recordings: sign-up posts that whitelist actors on the Minecraft server.

/recording-post   (staff)  opens a form (name, when, description, max actors) and posts an embed, in the
                           channel the command is used in, with a
                           "Join" button. The embed shows when it is, the description and a live count
                           like 3/10 needed actors.
Join button               asks for a Minecraft username, whitelists it over RCON (see rcon.py and the
                           RCON_* settings in .env), gives the actor role and replies "Thank you for
                           joining this recording!". An invalid username is reported to the user and
                           nothing is added. When the last slot is taken the button disappears and the
                           embed says the needed actors have been chosen.
/recording-end    (owners) once it's done: takes everyone who signed up off the whitelist (except people
                           who were already whitelisted before they signed up), removes their role, and
                           closes the post.
/recording-ban    (staff)  stops someone ever signing up as an actor (type "lift" as the reason to undo it).

Safety: a Minecraft username is checked against ^[A-Za-z0-9_]{3,16}$ before it goes anywhere near an RCON
command, so nobody can inject extra commands. A slot is held while someone's signup is in progress, so two
people pressing at once can never exceed the maximum.
"""
import asyncio
import logging
import re
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

import command_perms
import config
import database as db
import guild_config as gc
import rcon
from cogs.tickets import is_staff
from permissions import is_owner

USERNAME_RE = re.compile(r"[A-Za-z0-9_]{3,16}")
WHEN_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{2})\s*(?:UTC|GMT)?", re.I)
MAX_ACTORS_LIMIT = 500
BLURPLE, GREEN, GREY, RED = (discord.Color.blurple(), discord.Color.green(),
                             discord.Color.dark_grey(), discord.Color.red())

# How the server answers: words that mean the name wasn't a real player, or that our command was wrong.
INVALID_HINTS = ("does not exist", "doesn't exist", "not exist", "invalid", "no player", "not found", "cannot find",
                 "can't find", "could not find", "couldn't find", "unknown player", "no such player", "failed")
BROKEN_HINTS = ("unknown command", "unknown or incomplete", "usage:", "incorrect argument", "permission")


def classify_add(reply: str) -> str:
    """'ok' / 'already' (was whitelisted before) / 'invalid' (not a real username) / 'error' (bad command)."""
    low = reply.lower()
    if any(h in low for h in BROKEN_HINTS):
        return "error"
    if any(h in low for h in INVALID_HINTS):
        return "invalid"
    if "already" in low:
        return "already"
    return "ok"


def classify_remove(reply: str) -> str:
    """'ok' (they're off the list - including 'not whitelisted') or 'error'."""
    return "error" if any(h in reply.lower() for h in BROKEN_HINTS) else "ok"


def parse_when(text: str) -> int | None:
    """'2026-11-07 18:00' (UTC) -> unix time, so the post can show a live local-time stamp. Anything else
    (like 'Saturday 6pm EST') is shown exactly as typed."""
    m = WHEN_RE.fullmatch(text.strip())
    if not m:
        return None
    try:
        y, mo, d, h, mi = (int(x) for x in m.groups())
        return int(datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp())
    except ValueError:
        return None


def build_embed(rec, joined: int) -> discord.Embed:
    status = rec["status"]
    color = {"open": BLURPLE, "full": GREEN}.get(status, GREY)
    embed = discord.Embed(title=f"🎬 Upcoming Recording: {rec['title']}", description=rec["description"] or None, color=color)
    when = f"<t:{rec['when_ts']}:F>\n(<t:{rec['when_ts']}:R>)" if rec["when_ts"] else rec["when_text"]
    embed.add_field(name="🕒 When", value=when, inline=True)
    maximum = rec["max_actors"]
    filled = min(10, round(10 * joined / maximum)) if maximum else 0
    embed.add_field(name="🎭 Actors", value=f"**{joined}/{maximum}** needed actors\n{'🟩' * filled}{'⬛' * (10 - filled)}", inline=True)
    if status == "open":
        embed.add_field(name="Status", value="🟢 Signups are open - press the button below to join!", inline=False)
    elif status == "full":
        embed.add_field(name="Status", value="✅ The needed amount of actors have been chosen - signups are closed.", inline=False)
    else:
        embed.add_field(name="Status", value="🏁 This recording has ended.", inline=False)
    embed.set_footer(text=f"Recording #{rec['id']}")
    return embed


# ---------------- views / modals ----------------

class RecordingJoinView(discord.ui.View):
    """The sign-up button under a recording post. Persistent; the recording is found by its message."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Join as an actor", style=discord.ButtonStyle.success, emoji="🎬", custom_id="recording_join")
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.client.get_cog("Recordings").start_join(interaction)


class UsernameModal(discord.ui.Modal, title="Join this recording"):
    username = discord.ui.TextInput(label="Your Minecraft username", min_length=3, max_length=16,
                                    placeholder="Exactly as it appears in game")

    def __init__(self, cog: "Recordings", rec_id: int):
        super().__init__()
        self.cog, self.rec_id = cog, rec_id

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.finish_join(interaction, self.rec_id, str(self.username.value).strip())


class RecordingModal(discord.ui.Modal, title="New recording"):
    name = discord.ui.TextInput(label="Recording name", max_length=100, placeholder="e.g. Episode 12 - The Heist")
    when = discord.ui.TextInput(label="When is it?", max_length=100,
                                placeholder="2026-11-07 18:00 UTC  (or text like: Saturday 6pm EST)")
    description = discord.ui.TextInput(label="Description", style=discord.TextStyle.paragraph, max_length=1000,
                                       required=False, placeholder="What's the recording about? Any requirements?")
    max_actors = discord.ui.TextInput(label="Max actors needed", max_length=3, placeholder="e.g. 10")

    def __init__(self, cog: "Recordings", channel: discord.TextChannel):
        super().__init__()
        self.cog, self.channel = cog, channel

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.create_recording(interaction, self.channel, str(self.name.value).strip(), str(self.when.value).strip(),
                                        str(self.description.value).strip(), str(self.max_actors.value).strip())


class EndConfirmView(discord.ui.View):
    def __init__(self, cog: "Recordings", rec_id: int, owner_id: int):
        super().__init__(timeout=120)
        self.cog, self.rec_id, self.owner_id = cog, rec_id, owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    @discord.ui.button(label="Yes, clean up", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await self.cog.end_recording(interaction, self.rec_id)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(embed=discord.Embed(description="Cancelled - nothing was changed.", color=GREY), view=None)


# ---------------- the cog ----------------

class Recordings(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._locks: dict[int, asyncio.Lock] = {}
        bot.add_view(RecordingJoinView())

    async def cog_load(self):
        db.rec_clear_pending()  # signups interrupted by a restart must not hold slots forever

    def _lock(self, rec_id: int) -> asyncio.Lock:
        return self._locks.setdefault(rec_id, asyncio.Lock())

    @staticmethod
    def _error(text: str) -> discord.Embed:
        return discord.Embed(title="❌ Couldn't do that", description=text, color=RED)

    async def _staff_log(self, guild: discord.Guild, title: str, lines: list[str]):
        channel = gc.get_log_channel(guild, "staff-action-log-channel")
        if channel is None:
            return
        try:
            await channel.send(embed=discord.Embed(title=title, description="\n".join(lines)[:4000], color=BLURPLE,
                                                   timestamp=discord.utils.utcnow()),
                               allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass

    async def refresh_message(self, rec_id: int):
        """Re-renders the post: live count, and no button once it's full or ended."""
        rec = db.rec_get(rec_id)
        if rec is None or not rec["message_id"]:
            return
        channel = self.bot.get_channel(rec["channel_id"])
        if channel is None:
            return
        try:
            message = await channel.fetch_message(rec["message_id"])
            await message.edit(embed=build_embed(rec, db.rec_count(rec_id, joined_only=True)),
                               view=RecordingJoinView() if rec["status"] == "open" else None)
        except discord.HTTPException:
            logging.warning("couldn't update the post for recording %s", rec_id)

    # ---------------- /recording-post ----------------

    @app_commands.command(name="recording-post", description="[Staff] Post a recording sign-up in this channel (opens a form)")
    async def recording_post(self, interaction: discord.Interaction):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        target = interaction.channel  # always the channel the command is used in
        perms = target.permissions_for(interaction.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            return await interaction.response.send_message(f"I can't post embeds in {target.mention}.", ephemeral=True)
        await interaction.response.send_modal(RecordingModal(self, target))

    async def create_recording(self, interaction: discord.Interaction, channel: discord.TextChannel, title: str,
                               when_text: str, description: str, max_text: str):
        if not max_text.isdigit() or not 1 <= int(max_text) <= MAX_ACTORS_LIMIT:
            return await interaction.response.send_message(
                embed=self._error(f"**Max actors** must be a whole number from 1 to {MAX_ACTORS_LIMIT}."), ephemeral=True)
        rec_id = db.rec_create(interaction.guild.id, channel.id, title, when_text, parse_when(when_text),
                               description or None, int(max_text), interaction.user.id)
        rec = db.rec_get(rec_id)
        try:
            message = await channel.send(embed=build_embed(rec, 0), view=RecordingJoinView())
        except discord.HTTPException:
            db.rec_delete(rec_id)
            return await interaction.response.send_message(embed=self._error(f"I couldn't post in {channel.mention}."), ephemeral=True)
        db.rec_set_message(rec_id, channel.id, message.id)
        note = "" if rcon.configured() else "\n⚠️ RCON isn't configured yet (RCON_HOST / RCON_PASSWORD in `.env`), so signups will fail until it is."
        await interaction.response.send_message(f"✅ Recording #{rec_id} posted in {channel.mention}.{note}", ephemeral=True)
        await self._staff_log(interaction.guild, "🎬 Recording posted",
                              [f"**{title}** (#{rec_id}) by {interaction.user.mention} in {channel.mention}", f"Max actors: {max_text}"])

    # ---------------- the Join button ----------------

    async def start_join(self, interaction: discord.Interaction):
        guild, user = interaction.guild, interaction.user
        rec = db.rec_get_by_message(interaction.message.id)
        if rec is None:
            return await interaction.response.send_message("This recording post isn't active anymore.", ephemeral=True)
        if rec["status"] != "open":
            return await interaction.response.send_message("Signups for this recording are closed.", ephemeral=True)
        if db.rec_get_ban(guild.id, user.id) is not None:
            return await interaction.response.send_message("You're not able to sign up for recordings.", ephemeral=True)
        if db.rec_user_signup(rec["id"], user.id) is not None:
            return await interaction.response.send_message("You've already joined this recording!", ephemeral=True)
        if not rcon.configured():
            return await interaction.response.send_message(
                "Signups aren't set up on the server yet - please tell a staff member.", ephemeral=True)
        await interaction.response.send_modal(UsernameModal(self, rec["id"]))

    async def finish_join(self, interaction: discord.Interaction, rec_id: int, username: str):
        await interaction.response.defer(ephemeral=True)
        guild, user = interaction.guild, interaction.user

        async def say(text: str):
            await interaction.followup.send(text, ephemeral=True)

        if not USERNAME_RE.fullmatch(username):
            return await say("❌ That isn't a valid Minecraft username - it should be 3-16 characters: letters, numbers and underscores only.")

        async with self._lock(rec_id):  # reserve a slot so simultaneous presses can't overshoot the maximum
            rec = db.rec_get(rec_id)
            if rec is None or rec["status"] != "open":
                return await say("Signups for this recording are closed.")
            if db.rec_get_ban(guild.id, user.id) is not None:
                return await say("You're not able to sign up for recordings.")
            if db.rec_count(rec_id) >= rec["max_actors"]:
                return await say("Sorry - all the actor spots have just been taken.")
            if db.rec_username_taken(rec_id, username):
                return await say("❌ Someone has already signed up with that username.")
            signup_id = db.rec_add_signup(rec_id, user.id, username)
            if signup_id is None:
                return await say("You've already joined this recording!")

        try:
            reply = (await rcon.run([config.RCON_ADD_COMMAND.format(username=username)]))[0]
        except rcon.RconError as e:
            db.rec_signup_delete(signup_id)
            logging.error("recording signup: RCON failed for %s: %s", username, e)
            return await say("❌ I couldn't reach the Minecraft server right now. Please try again in a minute, or tell a staff member.")
        outcome = classify_add(reply)
        logging.info("recording %s signup %s -> %s (server said: %r)", rec_id, username, outcome, reply)
        if outcome == "invalid":
            db.rec_signup_delete(signup_id)
            return await say(f"❌ **{username}** isn't a valid Minecraft username, so I couldn't whitelist it. Check the spelling and press the button to try again.")
        if outcome == "error":
            db.rec_signup_delete(signup_id)
            return await say("❌ The server didn't accept the whitelist command - please tell a staff member.")

        db.rec_signup_joined(signup_id, already_wl=(outcome == "already"))
        role_problem = None
        role = guild.get_role(config.RECORDING_ACTOR_ROLE_ID)
        if role is None:
            role_problem = "the actor role doesn't exist"
        else:
            try:
                await user.add_roles(role, reason=f"Joined recording #{rec_id}")
            except discord.HTTPException:
                role_problem = "I'm missing permissions or my role is below the actor role"
        if role_problem:
            await self._staff_log(guild, "⚠️ Couldn't give the actor role",
                                  [f"{user.mention} joined recording #{rec_id} as `{username}`, but {role_problem}."])
        await say("Thank you for joining this recording!")

        async with self._lock(rec_id):  # close signups once the last slot is taken
            rec = db.rec_get(rec_id)
            if rec["status"] == "open" and db.rec_count(rec_id, joined_only=True) >= rec["max_actors"]:
                db.rec_set_status(rec_id, "full")
        await self.refresh_message(rec_id)

    # ---------------- /recording-end ----------------

    async def _recording_autocomplete(self, interaction: discord.Interaction, current: str):
        if not is_owner(interaction.user.id) or interaction.guild is None:
            return []
        out = []
        for rec in db.rec_unfinished(interaction.guild.id):
            label = f"#{rec['id']} {rec['title']} ({db.rec_count(rec['id'], joined_only=True)}/{rec['max_actors']})"
            if current.lower() in label.lower():
                out.append(app_commands.Choice(name=label[:100], value=rec["id"]))
        return out[:25]

    @app_commands.command(name="recording-end",
                          description="[Owner] End a recording: un-whitelist everyone who signed up and remove their role")
    @app_commands.describe(recording="Which recording (leave empty if only one is running)")
    @app_commands.autocomplete(recording=_recording_autocomplete)
    async def recording_end(self, interaction: discord.Interaction, recording: int = None):
        if not is_owner(interaction.user.id):
            return await interaction.response.send_message(command_perms.NO_PERMS_MESSAGE, ephemeral=True)
        guild = interaction.guild
        if recording is None:
            unfinished = db.rec_unfinished(guild.id)
            if not unfinished:
                return await interaction.response.send_message(embed=self._error("There's no recording to end."), ephemeral=True)
            if len(unfinished) > 1:
                names = "\n".join(f"#{r['id']} - {r['title']}" for r in unfinished[:10])
                return await interaction.response.send_message(
                    embed=self._error(f"More than one recording is running - pick one with the `recording` option:\n{names}"), ephemeral=True)
            rec = unfinished[0]
        else:
            rec = db.rec_get(recording)
            if rec is None or rec["guild_id"] != guild.id:
                return await interaction.response.send_message(embed=self._error("I couldn't find that recording."), ephemeral=True)
        signups = [s for s in db.rec_joined(rec["id"]) if not s["cleaned"]]
        to_remove = [s for s in signups if not s["already_wl"]]
        kept = [s for s in signups if s["already_wl"]]
        embed = discord.Embed(
            title=f"End recording #{rec['id']}: {rec['title']}?", color=discord.Color.orange(),
            description=f"**{len(signups)}** actor(s) signed up.\n"
                        f"• **{len(to_remove)}** will be taken off the whitelist\n"
                        f"• **{len(kept)}** stay whitelisted (they already were before signing up)\n"
                        f"• **{len(signups)}** will lose the actor role\n\nThis can't be undone.")
        await interaction.response.send_message(embed=embed, view=EndConfirmView(self, rec["id"], interaction.user.id), ephemeral=True)

    async def end_recording(self, interaction: discord.Interaction, rec_id: int):
        guild = interaction.guild
        rec = db.rec_get(rec_id)
        await interaction.response.edit_message(embed=discord.Embed(description="⏳ Cleaning up...", color=BLURPLE), view=None)
        signups = [s for s in db.rec_joined(rec_id) if not s["cleaned"]]
        to_remove = [s for s in signups if not s["already_wl"]]

        replies = []
        if to_remove:
            try:
                replies = await rcon.run([config.RCON_REMOVE_COMMAND.format(username=s["mc_username"]) for s in to_remove])
            except rcon.RconError as e:
                logging.error("recording end: RCON failed: %s", e)
                return await interaction.edit_original_response(embed=self._error(
                    f"I couldn't reach the Minecraft server ({e}). Nothing was changed - try again in a moment."))
        removal = {s["id"]: classify_remove(r) for s, r in zip(to_remove, replies)}

        role = guild.get_role(config.RECORDING_ACTOR_ROLE_ID)
        wl_removed, roles_removed, problems = 0, 0, []
        for s in signups:
            if not s["already_wl"]:
                if removal.get(s["id"]) != "ok":
                    problems.append(f"`{s['mc_username']}` - the server rejected the whitelist remove command")
                    continue  # leave them un-cleaned so running this again retries
                wl_removed += 1
            member = guild.get_member(s["user_id"])
            if member is not None and role is not None and role in member.roles:
                try:
                    await member.remove_roles(role, reason=f"Recording #{rec_id} ended")
                    roles_removed += 1
                except discord.HTTPException:
                    problems.append(f"{member.mention} - couldn't remove the role")
                    continue
            db.rec_signup_cleaned(s["id"])

        if not problems:
            db.rec_set_status(rec_id, "ended")
            await self.refresh_message(rec_id)
        result = discord.Embed(
            title=f"{'✅' if not problems else '⚠️'} Recording #{rec_id} {'ended' if not problems else 'partly cleaned up'}",
            color=GREEN if not problems else discord.Color.orange(),
            description=f"• Taken off the whitelist: **{wl_removed}**\n"
                        f"• Left on the whitelist (were already on it): **{sum(1 for s in signups if s['already_wl'])}**\n"
                        f"• Actor role removed: **{roles_removed}**")
        if problems:
            result.add_field(name="Needs another go", value="\n".join(problems)[:1024], inline=False)
            result.set_footer(text="Run /recording-end again to retry just these.")
        await interaction.edit_original_response(embed=result)
        await self._staff_log(guild, "🏁 Recording ended",
                              [f"**{rec['title']}** (#{rec_id}) cleaned up by {interaction.user.mention}",
                               f"Whitelist removed: {wl_removed} · roles removed: {roles_removed} · problems: {len(problems)}"])

    # ---------------- /recording-ban ----------------

    @app_commands.command(name="recording-ban", description="[Staff] Stop someone from ever signing up as a recording actor")
    @app_commands.describe(user="Mention or user ID", reason="Why - or type 'lift' to let them sign up again")
    async def recording_ban(self, interaction: discord.Interaction, user: discord.User, reason: str = "No reason given"):
        private = not getattr(interaction, "is_prefix", False)
        if not is_staff(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        guild = interaction.guild
        if reason.strip().lower() in ("lift", "unban", "remove"):
            if db.rec_unban(guild.id, user.id):
                await interaction.response.send_message(embed=discord.Embed(
                    description=f"✅ {user.mention} can sign up as an actor again.", color=GREEN), ephemeral=private)
                return await self._staff_log(guild, "🎬 Recording ban lifted", [f"{user.mention} by {interaction.user.mention}"])
            return await interaction.response.send_message(embed=self._error(f"{user.mention} isn't banned from recordings."), ephemeral=private)
        if user.bot or is_owner(user.id) or user.id == interaction.user.id:
            return await interaction.response.send_message(embed=self._error("You can't ban that user from recordings."), ephemeral=private)
        db.rec_ban(guild.id, user.id, reason, interaction.user.id)
        await interaction.response.send_message(embed=discord.Embed(
            description=f"🚫 {user.mention} can no longer sign up as a recording actor.\n**Reason:** {reason}", color=RED), ephemeral=private)
        await self._staff_log(guild, "🎬 Recording ban", [f"{user.mention} by {interaction.user.mention}", f"Reason: {reason}"])


async def setup(bot: commands.Bot):
    await bot.add_cog(Recordings(bot))
