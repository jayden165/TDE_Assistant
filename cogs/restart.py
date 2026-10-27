"""
/restart - restart the bot and re-read its env file (.env or tde.env) (bot owners only; also "!restart").

What it does
    1. Runs the same import check CI does (bot + every cog) in a separate process. If the new code or .env
       would stop the bot from starting, it says why and does NOT restart, so a typo can't take the bot down.
    2. Closes the Discord connection cleanly.
    3. Reads the env file again and puts its values into the environment, then starts the bot again in the
       same process (os.execv), so the new process starts with the fresh values. This works no matter how the
       bot was launched (pm2, systemd, a shell), because the new values are handed over directly.
       Which file: the path in the ENV_FILE variable if set, else tde.env, else .env, looked for next to
       bot.py and then one folder up.
    4. When it's back, posts "Back online" in the channel the command was used in.

It needs no pm2/systemd setup, and works under them too: the process id stays the same.
Only values from the env file are re-read. Variables injected by a host's dashboard can only be changed there.
"""
import asyncio
import logging
import os
import sys
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

import command_perms
import database as db
import guild_config as gc
from permissions import is_owner

ROOT = Path(__file__).resolve().parent.parent
NOTICE_KEY = "restart_notice"  # settings row (guild 0): where to say "back online"

# The same check the CI workflow runs.
IMPORT_CHECK = """
import importlib, pkgutil, sys
sys.path.insert(0, ".")
import bot
for m in pkgutil.iter_modules(["cogs"]):
    importlib.import_module("cogs." + m.name)
"""


async def import_check() -> str | None:
    """None if the bot imports fine, otherwise the last lines of the error."""
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", IMPORT_CHECK, cwd=str(ROOT),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=90)
    except asyncio.TimeoutError:
        return "The startup check took longer than 90 seconds."
    except Exception as e:
        return f"Couldn't run the startup check: {e}"
    if proc.returncode == 0:
        return None
    text = out.decode(errors="replace").strip().splitlines()
    return "\n".join(text[-8:])[-1500:] or "The startup check failed."


def find_env_file() -> Path | None:
    candidates = []
    if os.getenv("ENV_FILE"):
        candidates.append(Path(os.environ["ENV_FILE"]).expanduser())
    for folder in (ROOT, ROOT.parent):
        candidates += [folder / "tde.env", folder / ".env"]
    return next((p for p in candidates if p.is_file()), None)


def reload_env() -> Path | None:
    """Put the env file's values into os.environ (replacing the old ones). Returns the file used, or None."""
    path = find_env_file()
    if path is None:
        return None
    from dotenv import dotenv_values
    for key, value in dotenv_values(path).items():
        if value is not None:
            os.environ[key] = value
    return path


class Restart(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._announced = False

    # ---------------- after the restart ----------------

    @commands.Cog.listener()
    async def on_ready(self):
        if self._announced:
            return
        self._announced = True
        raw = db.get_setting(0, NOTICE_KEY)
        if not raw:
            return
        db.delete_setting(0, NOTICE_KEY)
        try:
            channel_id, user_id = (int(x) for x in str(raw).split(":"))
            channel = self.bot.get_channel(channel_id)
            if channel is not None:
                await channel.send(f"✅ <@{user_id}> I'm back online - the env file was re-read.",
                                   allowed_mentions=discord.AllowedMentions(users=True), delete_after=60)
        except Exception:
            logging.exception("couldn't post the 'back online' message")

    # ---------------- restarting ----------------

    async def _restart(self, user_id: int, channel_id: int | None, guild: discord.Guild | None, say):
        """`say(text)` sends a reply to whoever ran the command."""
        error = await import_check()
        if error:
            return await say(f"❌ I did **not** restart - the bot wouldn't start with the current files/.env:\n```{error}```")
        env_file = find_env_file()
        if env_file is None:
            return await say("❌ I did **not** restart - I couldn't find the env file (looked for `tde.env` and `.env` "
                             "next to bot.py and one folder up). Set `ENV_FILE=/full/path/to/tde.env` and try again.")
        await say(f"🔄 Restarting and re-reading `{env_file.name}`... I'll post here when I'm back.")
        logging.warning("restart requested by %s", user_id)
        await self._audit(guild, user_id)
        if channel_id:
            db.set_setting(0, NOTICE_KEY, f"{channel_id}:{user_id}")
        await asyncio.sleep(1)
        try:
            reload_env()
        except Exception as e:
            logging.exception("couldn't re-read the env file")
            return await say(f"❌ I did **not** restart - I couldn't read `{env_file.name}`: {e}")
        await self.bot.close()
        os.execv(sys.executable, [sys.executable, *sys.argv])

    async def _audit(self, guild: discord.Guild | None, user_id: int):
        """Note the restart in the staff action log. Never raises."""
        try:
            log = gc.get_log_channel(guild, "staff-action-log-channel") if guild else None
            if log is None:
                return
            embed = discord.Embed(title="🔄 Bot restart", color=discord.Color.orange(), timestamp=discord.utils.utcnow(),
                                  description=f"<@{user_id}> ({user_id}) restarted the bot and re-read the env file.")
            await log.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            logging.exception("couldn't write the restart audit log")

    @app_commands.command(name="restart", description="[Owner] Restart the bot and re-read its env file")
    async def restart_slash(self, interaction: discord.Interaction):
        if not is_owner(interaction.user.id):
            return await interaction.response.send_message(command_perms.NO_PERMS_MESSAGE, ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        await self._restart(interaction.user.id, interaction.channel_id, interaction.guild,
                            lambda text: interaction.followup.send(text, ephemeral=True))

    @commands.command(name="restart")
    async def restart_text(self, ctx: commands.Context):
        """Owner-only text version: !restart (handy if the slash commands aren't working)."""
        if not is_owner(ctx.author.id):
            return await ctx.reply(command_perms.NO_PERMS_MESSAGE)
        await self._restart(ctx.author.id, ctx.channel.id, ctx.guild, ctx.reply)


async def setup(bot: commands.Bot):
    await bot.add_cog(Restart(bot))
