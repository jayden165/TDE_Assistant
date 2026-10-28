"""
Bot-owner permissions.

ROOT_OWNER_ID is hardcoded on purpose: only that account can add or remove owners. Everyone on
the owner list (plus the root owner) passes every owner check and bypasses the bot's permission
checks. The list lives in the settings table (guild_id 0, key "owner_ids") as a JSON list.
"""
import json

import database as db

ROOT_OWNER_ID = 1347129235898237010
_KEY = "owner_ids"
_GLOBAL = 0  # settings rows with guild_id 0 aren't tied to a real server


def _stored() -> set[int]:
    raw = db.get_setting(_GLOBAL, _KEY, "[]")
    try:
        return {int(x) for x in json.loads(raw)}
    except (ValueError, TypeError):
        return set()


def owner_ids() -> set[int]:
    return _stored() | {ROOT_OWNER_ID}


def is_owner(user_id: int) -> bool:
    return user_id in owner_ids()


def is_root(user_id: int) -> bool:
    return user_id == ROOT_OWNER_ID


def add_owner(user_id: int) -> bool:
    """True if newly added."""
    ids = _stored()
    if user_id in ids or user_id == ROOT_OWNER_ID:
        return False
    ids.add(user_id)
    db.set_setting(_GLOBAL, _KEY, json.dumps(sorted(ids)))
    return True


def remove_owner(user_id: int) -> bool:
    """True if they were on the list. The root owner can't be removed."""
    ids = _stored()
    if user_id not in ids:
        return False
    ids.discard(user_id)
    db.set_setting(_GLOBAL, _KEY, json.dumps(sorted(ids)))
    return True


# ---------------- text-command prefixes ----------------
# Owners can register any number of prefixes (e.g. "s!", "TDE!", "DE?"); a message that starts
# with one runs the matching command, so "s! ping" works like /ping. See cogs/prefixcmds.py.

_PREFIX_KEY = "command_prefixes"
MAX_PREFIX_LENGTH = 20


def get_prefixes() -> list[str]:
    raw = db.get_setting(_GLOBAL, _PREFIX_KEY, "[]")
    try:
        return [str(p) for p in json.loads(raw) if str(p).strip()]
    except (ValueError, TypeError):
        return []


def _save_prefixes(prefixes: list[str]):
    db.set_setting(_GLOBAL, _PREFIX_KEY, json.dumps(prefixes))


def add_prefix(prefix: str) -> bool:
    prefixes = get_prefixes()
    if any(p.lower() == prefix.lower() for p in prefixes):
        return False
    prefixes.append(prefix)
    _save_prefixes(prefixes)
    return True


def remove_prefix(prefix: str) -> bool:
    prefixes = get_prefixes()
    kept = [p for p in prefixes if p.lower() != prefix.lower()]
    if len(kept) == len(prefixes):
        return False
    _save_prefixes(kept)
    return True


# ---------------- /set ----------------
# The one account allowed to change the bot's IDs (server, roles, channels...) with /set.
SETUP_USER_ID = 1560257342287777813


def can_set(user_id: int) -> bool:
    return user_id == SETUP_USER_ID
