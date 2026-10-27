"""
Which support system members use: tickets (the panel dropdown opens a private channel), modmail
(members DM the bot, or pick the modmail entry - "Video Suggestion" - in the panel dropdown, and staff
answer in a private channel), or both. Stored per server; switched with /support-mode. Both is the
default, so the Video Suggestion option shows up in the ticket dropdown out of the box.

Switching only affects NEW conversations - open tickets and open modmail threads keep working.
"""
import database as db

TICKETS, MODMAIL, BOTH = "tickets", "modmail", "both"
MODES = (TICKETS, MODMAIL, BOTH)
DEFAULT = BOTH
LABELS = {TICKETS: "🎫 Tickets", MODMAIL: "📬 Modmail", BOTH: "🎫📬 Tickets and modmail"}
_KEY = "support_mode"
_LABEL_KEY = "modmail_panel_label"
DEFAULT_MODMAIL_LABEL = "Video Suggestion"  # the modmail entry in the ticket panel's dropdown


def get_mode(guild_id: int) -> str:
    value = db.get_setting(guild_id, _KEY, DEFAULT)
    return value if value in MODES else DEFAULT


def set_mode(guild_id: int, mode: str):
    if mode not in MODES:
        raise ValueError(mode)
    db.set_setting(guild_id, _KEY, mode)


def allows_tickets(mode: str) -> bool:
    return mode in (TICKETS, BOTH)


def allows_modmail(mode: str) -> bool:
    return mode in (MODMAIL, BOTH)


def get_modmail_label(guild_id: int) -> str:
    """Name of the modmail option in the ticket panel dropdown."""
    return (db.get_setting(guild_id, _LABEL_KEY, DEFAULT_MODMAIL_LABEL) or DEFAULT_MODMAIL_LABEL)[:100]


def set_modmail_label(guild_id: int, label: str):
    db.set_setting(guild_id, _LABEL_KEY, label.strip()[:100])
