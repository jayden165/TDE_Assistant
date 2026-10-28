"""
Config values are read from environment variables first (so you can set them in
your host's dashboard / a .env file without putting secrets in the repo), and
fall back to the literal defaults below for local testing.

Every ID default below is None; set them in .env. Anything gated on a None ID just no-ops
or reports itself as "not configured yet" in /debug.

Note: several settings described as "set by a command at runtime" (welcome
channel/message, honeypot channels, channel locks) are NOT here - they're stored in the database via the settings table so they can
be changed without redeploying. This file is only for things that rarely change (IDs,
static text, tunables).
"""
import os


def _int(name: str, default=None):
    val = os.getenv(name)
    try:
        return int(val) if val else default
    except ValueError:
        return default


def _str(name: str, default: str) -> str:
    return os.getenv(name, default)


# ---- Core ----
TOKEN = _str("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
GUILD_ID = _int("GUILD_ID")  # the one server this bot runs in; commands sync there instantly (global sync if unset)

# ---- Command rate limit (normal members only; staff/admins/owners are exempt) ----
MEMBER_COMMAND_LIMIT = _int("MEMBER_COMMAND_LIMIT", 5)      # commands allowed per window; 0 turns the limit off
MEMBER_COMMAND_WINDOW = _int("MEMBER_COMMAND_WINDOW", 30)   # window length in seconds

# ---- Minecraft server (recordings: /recording-post, /recording-end) ----
# RCON is how the bot whitelists people who sign up. Put the password ONLY in .env, never in the code.
RCON_HOST = os.getenv("RCON_HOST", "")
RCON_PORT = _int("RCON_PORT", 25575)
RCON_PASSWORD = os.getenv("RCON_PASSWORD", "")
RCON_ADD_COMMAND = os.getenv("RCON_ADD_COMMAND", "wl {username}")                    # sent to whitelist someone
RCON_REMOVE_COMMAND = os.getenv("RCON_REMOVE_COMMAND", "whitelist remove {username}")  # sent by /recording-end
RECORDING_ACTOR_ROLE_ID = _int("RECORDING_ACTOR_ROLE_ID", 1563819351034036224)        # given to actors who sign up

# ---- YouTube upload notifier (/upload channel set) ----
YOUTUBE_CHANNEL_HANDLE = os.getenv("YOUTUBE_CHANNEL_HANDLE", "@thedailyemeraldmc")
YOUTUBE_CHANNEL_ID = os.getenv("YOUTUBE_CHANNEL_ID", "")        # optional; looked up from the handle if empty
YOUTUBE_CHANNEL_NAME = os.getenv("YOUTUBE_CHANNEL_NAME", "The Daily Emerald")
YOUTUBE_POLL_MINUTES = max(2, _int("YOUTUBE_POLL_MINUTES", 5))   # how often to check for new uploads
YOUTUBE_UPDATES_CHANNEL_ID = _int("YOUTUBE_UPDATES_CHANNEL_ID")  # default channel (normally set with /upload channel set)
YOUTUBE_PING_ROLE_ID = _int("YOUTUBE_PING_ROLE_ID")              # optional role to ping on uploads

# ---- Global ban (a ban in any server the bot is in is mirrored to all the others) ----
def _flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    return default if value is None or value.strip() == "" else value.strip().lower() in ("1", "true", "yes", "on")


GLOBAL_BAN_ENABLED = _flag("GLOBAL_BAN_ENABLED", True)
GLOBAL_UNBAN_ALL = _flag("GLOBAL_UNBAN_ALL", False)  # True: an unban anywhere lifts EVERY ban of that user, even ones a server made on its own

# ---- YouTube upload notifications (/upload) ----
YOUTUBE_CHANNEL_ID = os.getenv("YOUTUBE_CHANNEL_ID", "UCTHIdKaVTME8g-6KHcCW-Jg")     # The Daily Emerald
YOUTUBE_CHANNEL_HANDLE = os.getenv("YOUTUBE_CHANNEL_HANDLE", "thedailyemeraldmc")
YOUTUBE_CHANNEL_NAME = os.getenv("YOUTUBE_CHANNEL_NAME", "The Daily Emerald")
YOUTUBE_POLL_MINUTES = _int("YOUTUBE_POLL_MINUTES", 5)

# ---- Developer ----
# Gets the DMs from /customcmd request. Falls back to the setup user in permissions.py if unset.
DEV_USER_ID = _int("DEV_USER_ID")

# ---- Bot owners ----
# Managed in-bot with /owner (see permissions.py); the root owner is hardcoded there.

# ---- Roles ----
STAFF_ROLE_ID = _int("STAFF_ROLE_ID")
ADMIN_ROLE_ID = _int("ADMIN_ROLE_ID")

# ---- Applications ----
APPLICATION_LOG_CHANNEL_ID = _int("APPLICATION_LOG_CHANNEL_ID")
# Optional: when set, a decided application is moved out of the review channel into these.
APPLICATION_ACCEPTED_CHANNEL_ID = _int("APPLICATION_ACCEPTED_CHANNEL_ID")
APPLICATION_DENIED_CHANNEL_ID = _int("APPLICATION_DENIED_CHANNEL_ID")

LOG_CHANNEL_ID = _int("LOG_CHANNEL_ID")  # unified log channel - all bot logs go here

# Optional dedicated log channels. If left unset, these fall back to the unified log channel above.
MOD_LOG_CHANNEL_ID = _int("MOD_LOG_CHANNEL_ID")        # bans, unbans, kicks, timeouts, mutes - by anyone

# ---- Ticket System ----
TICKET_CATEGORY_ID = _int("TICKET_CATEGORY_ID")  # fallback for applications and any type without its own category below
TICKET_CATEGORY_GENERAL_ID = _int("TICKET_CATEGORY_GENERAL_ID")
TICKET_CATEGORY_REPORT_ID = _int("TICKET_CATEGORY_REPORT_ID")
TICKET_CATEGORY_EVENTS_ID = _int("TICKET_CATEGORY_EVENTS_ID")
TICKET_CATEGORY_ADMIN_ID = _int("TICKET_CATEGORY_ADMIN_ID")
TICKET_PANEL_CHANNEL_ID = _int("TICKET_PANEL_CHANNEL_ID")
TRANSCRIPT_CHANNEL_ID = _int("TRANSCRIPT_CHANNEL_ID")

STAFF_ACTION_LOG_CHANNEL_ID = _int("STAFF_ACTION_LOG_CHANNEL_ID")  # purge log

# ---- Moderation tools ----
MUTE_ROLE_ID = _int("MUTE_ROLE_ID")

# ---- Modmail ----
MODMAIL_CATEGORY_ID = _int("MODMAIL_CATEGORY_ID")            # category for Video Suggestion (modmail) threads (falls back to TICKET_CATEGORY_ID)
MODMAIL_LOG_CHANNEL_ID = _int("MODMAIL_LOG_CHANNEL_ID")      # transcripts of closed modmail (falls back to the transcript channel)

# ---- Anti-raid ----
ANTIRAID_LOG_CHANNEL_ID = _int("ANTIRAID_LOG_CHANNEL_ID")  # falls back to LOG_CHANNEL_ID if unset

# ---- Honeypot ----
HONEYPOT_LOG_CHANNEL_ID = _int("HONEYPOT_LOG_CHANNEL_ID")
