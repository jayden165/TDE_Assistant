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
TICKET_CATEGORY_ID = _int("TICKET_CATEGORY_ID")
TICKET_PANEL_CHANNEL_ID = _int("TICKET_PANEL_CHANNEL_ID")
TRANSCRIPT_CHANNEL_ID = _int("TRANSCRIPT_CHANNEL_ID")

STAFF_ACTION_LOG_CHANNEL_ID = _int("STAFF_ACTION_LOG_CHANNEL_ID")  # purge log

# ---- Moderation tools ----
MUTE_ROLE_ID = _int("MUTE_ROLE_ID")

RULES_URL = _str("RULES_URL", "")  # optional - if set, /rules adds a "View Full Rules" link button

# ---- Anti-raid ----
ANTIRAID_LOG_CHANNEL_ID = _int("ANTIRAID_LOG_CHANNEL_ID")  # falls back to LOG_CHANNEL_ID if unset

# ---- Honeypot ----
HONEYPOT_LOG_CHANNEL_ID = _int("HONEYPOT_LOG_CHANNEL_ID")
