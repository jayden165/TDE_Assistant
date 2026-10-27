"""
/status - the bot's status, set by bot owners only (slash or text: "M! status watching the server"; add remove:True to delete one).

A status is an activity line under the bot's name: Playing / Watching / Listening to / Competing in + text.
Add as many as you like. With one, it just stays; with more than one they rotate every minute. `text`
autocompletes from the current list. Statuses are saved, so they survive restarts. (Discord doesn't let bots
show a "custom status" - bots only get these activity types.)
"""
import json
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

import command_perms
import database as db
from permissions import is_owner

SETTING_KEY = "bot_statuses"
MAX_STATUSES = 20
MAX_TEXT = 128  # Discord's limit for an activity name
ROTATE_SECONDS = 60

ACTIVITY_TYPES = {
    "playing": (discord.ActivityType.playing, "Playing"),
    "watching": (discord.ActivityType.watching, "Watching"),
    "listening": (discord.ActivityType.listening, "Listening to"),
    "competing": (discord.ActivityType.competing, "Competing in"),
}


def load() -> list[dict]:
    try:
        data = json.loads(db.get_setting(0, SETTING_KEY, "[]"))
    except (ValueError, TypeError):
        return []
    return [s for s in data if isinstance(s, dict) and s.get("type") in ACTIVITY_TYPES and s.get("text")]


def save(statuses: list[dict]):
    db.set_setting(0, SETTING_KEY, json.dumps(statuses))


def label(status: dict) -> str:
    return f"{ACTIVITY_TYPES[status['type']][1]} {status['text']}"


def listing(statuses: list[dict]) -> str:
    if not statuses:
        return "No statuses set - the bot shows none."
    note = f"(rotating every {ROTATE_SECONDS} seconds)" if len(statuses) > 1 else "(stays on this one)"
    return f"**Statuses** {note}\n" + "\n".join(f"{i}. {label(s)}" for i, s in enumerate(statuses, start=1))


class Status(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._i = 0  # which status is next

    async def cog_load(self):
        self.rotate.start()

    async def cog_unload(self):
        self.rotate.cancel()

    # ---------------- applying ----------------

    @tasks.loop(seconds=ROTATE_SECONDS)
    async def rotate(self):
        statuses = load()
        try:
            if not statuses:
                await self.bot.change_presence(activity=None)
                return
            status = statuses[self._i % len(statuses)]
            self._i = (self._i + 1) % len(statuses)
            await self.bot.change_presence(activity=discord.Activity(type=ACTIVITY_TYPES[status["type"]][0], name=status["text"]))
        except Exception:
            logging.exception("couldn't update the bot's status")

    @rotate.before_loop
    async def _wait(self):
        await self.bot.wait_until_ready()

    # ---------------- /status ----------------

    @staticmethod
    def _reply(text: str, ok: bool = True) -> discord.Embed:
        return discord.Embed(description=text, color=discord.Color.green() if ok else discord.Color.red())

    @app_commands.command(name="status", description="[Owner] Add a bot status (several rotate every minute), remove one, or list them")
    @app_commands.describe(activity="What the bot is doing (needed to add a status)",
                           text="The status text (leave empty to list your statuses)",
                           remove="Remove the status with this text instead of adding one")
    @app_commands.choices(activity=[app_commands.Choice(name=name, value=key) for key, (_t, name) in ACTIVITY_TYPES.items()])
    async def status(self, interaction: discord.Interaction, activity: app_commands.Choice[str] = None,
                     text: str = None, remove: bool = False):
        private = not getattr(interaction, "is_prefix", False)
        if not is_owner(interaction.user.id):
            return await interaction.response.send_message(command_perms.NO_PERMS_MESSAGE, ephemeral=True)
        statuses = load()
        if text is None:  # nothing to add or remove: show the list
            return await interaction.response.send_message(embed=self._reply(listing(statuses)), ephemeral=private)
        text = text.strip()
        if remove:
            wanted = text.lower()
            # accept the bare text, or the full "Watching the server" label shown in the list
            match = next((s for s in statuses if s["text"].lower() == wanted or label(s).lower() == wanted), None)
            if match is None:
                return await interaction.response.send_message(
                    embed=self._reply(f"I couldn't find that status.\n\n{listing(statuses)}", ok=False), ephemeral=private)
            statuses.remove(match)
            save(statuses)
            self._i = 0
            self.rotate.restart()
            return await interaction.response.send_message(
                embed=self._reply(f"🗑️ Removed **{label(match)}**\n\n{listing(statuses)}"), ephemeral=private)
        if activity is None:
            return await interaction.response.send_message(
                embed=self._reply("Pick an `activity` (Playing, Watching...) to add a status.", ok=False), ephemeral=private)
        if not text or len(text) > MAX_TEXT:
            return await interaction.response.send_message(
                embed=self._reply(f"The status text must be 1-{MAX_TEXT} characters.", ok=False), ephemeral=private)
        if len(statuses) >= MAX_STATUSES:
            return await interaction.response.send_message(
                embed=self._reply(f"That's the maximum of {MAX_STATUSES} statuses - remove one first.", ok=False), ephemeral=private)
        if any(s["text"].lower() == text.lower() for s in statuses):
            return await interaction.response.send_message(
                embed=self._reply("A status with that text already exists.", ok=False), ephemeral=private)
        statuses.append({"type": activity.value, "text": text})
        save(statuses)
        self._i = len(statuses) - 1  # show the new one straight away, then keep rotating
        self.rotate.restart()
        await interaction.response.send_message(
            embed=self._reply(f"✅ Added **{label(statuses[-1])}**\n\n{listing(statuses)}"), ephemeral=private)

    @status.autocomplete("text")
    async def _remove_autocomplete(self, interaction: discord.Interaction, current: str):
        if not is_owner(interaction.user.id):
            return []
        current = current.lower()
        return [app_commands.Choice(name=label(s)[:100], value=s["text"][:100])
                for s in load() if current in label(s).lower()][:25]


async def setup(bot: commands.Bot):
    await bot.add_cog(Status(bot))
