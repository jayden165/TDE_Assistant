"""
Channel/role IDs: read from config.py/.env, with optional overrides saved in the database
(e.g. by /antiraid config) taking priority.

Storage is the generic `settings` table (guild_id, key, value) via database.py's
get_setting/set_setting - each override is stored under the key "cfg:<setting-key>".
"""
import config
import database as db

# setting key -> (human label, config.py fallback attribute, expected channel type or None)
CHANNEL_SETTINGS = {
    "log-channel": ("Unified Log Channel", "LOG_CHANNEL_ID", None),
    "mod-log-channel": ("Moderation Log Channel (bans, kicks, timeouts, mutes...)", "MOD_LOG_CHANNEL_ID", None),
    "application-log-channel": ("Application Log Channel", "APPLICATION_LOG_CHANNEL_ID", None),
    "application-accepted-channel": ("Applications Accepted Channel", "APPLICATION_ACCEPTED_CHANNEL_ID", None),
    "application-denied-channel": ("Applications Denied Channel", "APPLICATION_DENIED_CHANNEL_ID", None),
    "ticket-panel": ("Ticket Panel Channel", "TICKET_PANEL_CHANNEL_ID", None),
    "transcript-channel": ("Transcript Channel", "TRANSCRIPT_CHANNEL_ID", None),
    "staff-action-log-channel": ("Staff Action Log Channel", "STAFF_ACTION_LOG_CHANNEL_ID", None),
    "honeypot-log-channel": ("Honeypot Log Channel (default)", "HONEYPOT_LOG_CHANNEL_ID", None),
    "antiraid-log-channel": ("Anti-Raid Log Channel", "ANTIRAID_LOG_CHANNEL_ID", None),
}

ROLE_SETTINGS = {
    "staff-role": ("Staff Role", "STAFF_ROLE_ID"),
    "admin-role": ("Admin Role", "ADMIN_ROLE_ID"),
    "mute-role": ("Mute Role", "MUTE_ROLE_ID"),
}

CATEGORY_SETTINGS = {
    "ticket-category": ("Ticket Category", "TICKET_CATEGORY_ID"),
}

# Log channels that fall back to another one when they aren't configured, so a server that
# hasn't set the dedicated channel keeps getting those logs in the unified log channel.
LOG_FALLBACKS = {
    "mod-log-channel": "log-channel",
    "antiraid-log-channel": "log-channel",
}

ALL_ID_SETTINGS = {**CHANNEL_SETTINGS, **ROLE_SETTINGS, **CATEGORY_SETTINGS}


def _default_for(key: str) -> int:
    entry = ALL_ID_SETTINGS[key]
    attr = entry[1]
    if attr:
        return getattr(config, attr, 0) or 0
    return 0


def get(guild_id: int, key: str) -> int:
    """Resolved ID for this setting in this guild - the saved override if one exists,
    otherwise the config.py/.env default. Returns 0 if neither is set."""
    raw = db.get_setting(guild_id, f"cfg:{key}")
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return _default_for(key)


def is_overridden(guild_id: int, key: str) -> bool:
    return db.get_setting(guild_id, f"cfg:{key}") is not None


def set_override(guild_id: int, key: str, value: int):
    db.set_setting(guild_id, f"cfg:{key}", value)


def clear_override(guild_id: int, key: str):
    db.delete_setting(guild_id, f"cfg:{key}")


def get_log_channel_id(guild_id: int, key: str) -> int:
    """Channel ID to send a log of this kind to. Uses the setting's own channel if one is set,
    otherwise falls back to the unified log channel (see LOG_FALLBACKS). 0 if neither is set."""
    value = get(guild_id, key)
    if value:
        return value
    fallback = LOG_FALLBACKS.get(key)
    return get(guild_id, fallback) if fallback else 0


def get_log_channel(guild, key: str):
    """The actual channel object for a log kind in this guild, or None. If the dedicated
    channel was set but has since been deleted, falls back to the unified log channel too."""
    for candidate in (key, LOG_FALLBACKS.get(key)):
        if not candidate:
            continue
        channel_id = get(guild.id, candidate)
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel is not None:
            return channel
    return None
