import datetime
import logging

import discord
from discord.ext import commands

try:
    from dotenv import load_dotenv
    load_dotenv()  # loads a local .env file if present; no-op on hosts that inject env vars directly
except ImportError:
    pass

import command_perms
import config
import database as db
from permissions import is_owner

logging.basicConfig(level=logging.INFO)

INTENTS = discord.Intents.default()
INTENTS.members = True
INTENTS.message_content = True

EXTENSIONS = [
    "cogs.tickets",
    "cogs.moderation_tools",
    "cogs.applications",
    "cogs.utility",
    "cogs.honeypot",
    "cogs.debug",
    "cogs.modlog",
    "cogs.antiraid",
    "cogs.owner",
    "cogs.prefixcmds",
    "cogs.punishments",
    "cogs.log_aio",
    "cogs.custom_commands",
    "cogs.prefix_help",
    "cogs.channel_settings",
    "cogs.modmail",
    "cogs.recordings",
    "cogs.globalban",
    "cogs.status",
    "cogs.uploads",
    "cogs.uploads",
    "cogs.settings_cmd",
]

# Commands hidden from anyone without the matching Discord permission (Discord's own
# command-visibility system). The in-code permission checks still run as a second layer.
# /owner is deliberately not listed: owners aren't necessarily administrators, and its
# handler refuses everyone who isn't on the owner list.
ADMIN_ONLY_COMMANDS = [
    "role", "embed", "moderation-panel", "welcome", "honeypot", "antiraid",
    "debug", "log-aio", "support-mode", "upload", "upload",
]

MANAGE_MESSAGES_COMMANDS = [
    "panel", "infractions", "channel", "applications", "recording-post", "recording-ban",
]


# Moderation commands: hidden from anyone without the matching Discord permission.
MODERATION_COMMAND_PERMS = {
    "ban": "ban_members", "unban": "ban_members", "kick": "kick_members",
    "timeout": "moderate_members", "warn": "moderate_members", "mute": "moderate_members",
}


class TDEAssistant(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=INTENTS, help_command=None,
                         tree_cls=command_perms.GatedTree)
        self.start_time = datetime.datetime.now(datetime.timezone.utc)
        self._synced_once = False

    async def setup_hook(self):
        db.init_db()
        command_perms.migrate_renamed_rules()
        failed = []
        for ext in EXTENSIONS:
            try:
                await self.load_extension(ext)
                logging.info(f"Loaded {ext}")
            except Exception:
                # A cog that fails to import silently loses all of its commands, so log the traceback.
                logging.exception(f"FAILED TO LOAD {ext}")
                failed.append(ext)

        if failed:
            logging.error("=" * 60)
            logging.error(f"{len(failed)} cog(s) failed to load: {', '.join(failed)}")
            logging.error("Their commands will NOT appear in Discord. See tracebacks above.")
            logging.error("=" * 60)

        self._apply_command_permissions()

    def _apply_command_permissions(self):
        """Sets Discord's own command visibility. Commands that an owner has configured with
        /commandperms are shown to everyone (the in-code gate decides who can actually run
        them), otherwise a granted role couldn't even see the command in the picker."""
        admin_perms = discord.Permissions(administrator=True)
        staff_perms = discord.Permissions(manage_messages=True)
        for cmd in self.tree.get_commands():
            try:
                if cmd.name in command_perms.STRICT_OWNER_ONLY:
                    # Owners aren't necessarily administrators; the handler refuses everyone else.
                    cmd.default_permissions = None
                elif config.GUILD_ID and command_perms.has_any_rule(config.GUILD_ID, cmd.name):
                    cmd.default_permissions = None
                elif cmd.name in MODERATION_COMMAND_PERMS:
                    cmd.default_permissions = discord.Permissions(**{MODERATION_COMMAND_PERMS[cmd.name]: True})
                elif cmd.name in ADMIN_ONLY_COMMANDS or cmd.name in command_perms.OWNER_ONLY_DEFAULT:
                    cmd.default_permissions = admin_perms
                elif cmd.name in MANAGE_MESSAGES_COMMANDS:
                    cmd.default_permissions = staff_perms
                else:
                    cmd.default_permissions = None
            except Exception as e:
                logging.warning(f"Couldn't set default permissions on /{cmd.name}: {e}")

    async def sync_commands(self):
        """Syncs slash commands to GUILD_ID (instant) if set, otherwise globally."""
        if config.GUILD_ID:
            guild = discord.Object(id=config.GUILD_ID)
            self.tree.clear_commands(guild=guild)  # drop stale copies so permission changes carry over
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_ready(self):
        logging.info(f"Logged in as {self.user} ({self.user.id})")
        if self._synced_once:
            return
        self._synced_once = True
        try:
            await self.sync_commands()
            logging.info("Command sync complete.")
        except discord.HTTPException:
            logging.exception("Command sync failed")


@commands.command(name="sync")
async def sync_prefix(ctx: commands.Context):
    """Owner-only text fallback: !sync re-syncs the slash commands."""
    if not is_owner(ctx.author.id):
        return await ctx.reply("you dont have the perms")
    try:
        await ctx.bot.sync_commands()
        await ctx.reply("Synced commands.")
    except discord.HTTPException as e:
        await ctx.reply(f"Sync failed: {e}")


bot = TDEAssistant()
bot.add_command(sync_prefix)

if __name__ == "__main__":
    if not config.TOKEN or config.TOKEN == "YOUR_BOT_TOKEN_HERE":
        raise SystemExit("Set BOT_TOKEN in your .env file before running.")
    bot.run(config.TOKEN)
