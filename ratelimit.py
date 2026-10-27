"""
Command rate limit for normal members.

Anyone who isn't staff can run at most MEMBER_COMMAND_LIMIT commands (slash or text, including custom
commands) per MEMBER_COMMAND_WINDOW seconds - default 5 per 30s. Going over gets a "slow down" notice
with how long to wait; the command doesn't run and doesn't count. Staff, admins, moderators and bot
owners are never limited. Set MEMBER_COMMAND_LIMIT=0 in .env to turn it off. Panels, dropdowns, modmail
and application DMs aren't commands and aren't counted.
"""
import collections
import time

import config
import guild_config as gc
from permissions import is_owner

_uses: dict[int, collections.deque] = collections.defaultdict(collections.deque)


def exempt(member) -> bool:
    if is_owner(member.id):
        return True
    perms = getattr(member, "guild_permissions", None)
    if perms is not None and (perms.administrator or perms.manage_messages or perms.moderate_members
                              or perms.ban_members or perms.kick_members):
        return True
    guild = getattr(member, "guild", None)
    if guild is None:
        return False
    role_ids = {r.id for r in getattr(member, "roles", [])}
    return gc.get(guild.id, "staff-role") in role_ids or gc.get(guild.id, "admin-role") in role_ids


def check(member) -> float | None:
    """Counts one command use. None = allowed; otherwise how many seconds until they can run another."""
    limit, window = config.MEMBER_COMMAND_LIMIT, config.MEMBER_COMMAND_WINDOW
    if limit <= 0 or window <= 0 or exempt(member):
        return None
    now = time.monotonic()
    uses = _uses[member.id]
    while uses and now - uses[0] >= window:
        uses.popleft()
    if len(uses) >= limit:
        return max(1.0, window - (now - uses[0]))
    uses.append(now)
    return None


def notice(wait: float) -> str:
    return f"⏳ Slow down - you can use commands again in **{int(wait) + 1}s**."
