"""
Per-command role permissions, managed by owners with /commandperms.

How it works
------------
* Every command can have a list of allowed roles, stored per server in the settings table under
  the key "cmdperm:<command name>" (e.g. "cmdperm:purge", "cmdperm:role add").
* A rule on a command group (e.g. "role") covers all of its subcommands; a rule on one subcommand
  (e.g. "role add") takes priority over the group's rule.
* If a command HAS a rule, only bot owners and members holding one of the listed roles can use it.
  That replaces the command's built-in requirement (admin / staff), so it can be used to open a
  command up to a new rank *or* to lock it down to a smaller one.
* If a command has NO rule it behaves as before, except the commands in OWNER_ONLY_DEFAULT
  (/embed), which is owner-only until an owner grants a role. /say is stricter:
  owner-only always, and /commandperms can't change that (see STRICT_OWNER_ONLY).
* Bot owners always pass.

The gate runs in GatedTree.interaction_check for slash commands and from cogs/prefixcmds.py for text
commands. Each cog still has its own inline admin/staff check, so when a role grant lets someone
through the gate we flip a context variable that those checks honour (see granted()).
"""
import contextvars
import json

import discord
from discord import app_commands

import database as db
import ratelimit
from permissions import is_owner

# Owner-only until an owner explicitly grants roles with /commandperms.
OWNER_ONLY_DEFAULT = frozenset({"embed"})

# Owner-only, always: no role rule (and no Discord permission) can open these up.
STRICT_OWNER_ONLY = frozenset({"say", "dm", "insmp", "customcmd", "setchannel", "recording-end", "status", "restart"})

# Hard-wired to the owner list / setup user in their own handlers; role rules can't change them.
NOT_CONFIGURABLE = frozenset({"owner", "commandperms", "prefix", "set"}) | STRICT_OWNER_ONLY

NO_PERMS_MESSAGE = "you dont have the perms"

_PREFIX = "cmdperm:"

ALLOW = "allow"      # an owner, or holds a role granted for this command
DENY = "deny"        # blocked by a rule (or by the owner-only default)
DEFAULT = "default"  # no rule: the command's own built-in check decides

_granted = contextvars.ContextVar("cmdperm_granted", default=False)


def granted() -> bool:
    """True while running a command that a role grant (or ownership) has already cleared.
    The inline is_admin_perm()/is_staff() helpers in the cogs return True when this is set."""
    return _granted.get()


def set_granted(value: bool):
    return _granted.set(value)


def reset_granted(token):
    _granted.reset(token)


# ---------------- storage ----------------

def normalize(name: str) -> str:
    return " ".join((name or "").lower().lstrip("/").split())


def _key(name: str) -> str:
    return _PREFIX + normalize(name)


def get_roles(guild_id: int, name: str):
    """Role IDs allowed for exactly this command name, or None if it has no rule."""
    raw = db.get_setting(guild_id, _key(name))
    if raw is None:
        return None
    try:
        ids = [int(x) for x in json.loads(raw)]
    except (ValueError, TypeError):
        return None
    return ids or None


def _save(guild_id: int, name: str, ids: list[int]):
    if ids:
        db.set_setting(guild_id, _key(name), json.dumps(sorted(set(ids))))
    else:
        db.delete_setting(guild_id, _key(name))


def add_role(guild_id: int, name: str, role_id: int) -> bool:
    """True if newly added."""
    ids = get_roles(guild_id, name) or []
    if role_id in ids:
        return False
    _save(guild_id, name, ids + [role_id])
    return True


def remove_role(guild_id: int, name: str, role_id: int) -> bool:
    """True if the role was on the list. Removing the last role deletes the rule."""
    ids = get_roles(guild_id, name) or []
    if role_id not in ids:
        return False
    _save(guild_id, name, [i for i in ids if i != role_id])
    return True


def reset(guild_id: int, name: str) -> bool:
    """Drops the rule so the command goes back to its default. True if there was one."""
    existed = get_roles(guild_id, name) is not None
    db.delete_setting(guild_id, _key(name))
    return existed


def all_rules(guild_id: int) -> dict[str, list[int]]:
    rules = {}
    for row in db.get_settings_by_prefix(guild_id, _PREFIX):
        name = row["key"][len(_PREFIX):]
        ids = get_roles(guild_id, name)
        if ids:
            rules[name] = ids
    return rules


def has_any_rule(guild_id: int, top_level_name: str) -> bool:
    """True if this command, or any of its subcommands, has a rule."""
    name = normalize(top_level_name)
    return any(r == name or r.startswith(name + " ") for r in all_rules(guild_id))


# ---------------- evaluation ----------------

def effective_roles(guild_id: int, qualified_name: str):
    """The role list that applies to a command: its own rule, else its group's. None if neither."""
    name = normalize(qualified_name)
    ids = get_roles(guild_id, name)
    if ids is None and " " in name:
        ids = get_roles(guild_id, name.split(" ", 1)[0])
    return ids


def evaluate(user, guild_id, qualified_name: str) -> str:
    """ALLOW / DENY / DEFAULT for this user running this command."""
    if is_owner(user.id):
        return ALLOW
    if normalize(qualified_name).split(" ", 1)[0] in STRICT_OWNER_ONLY:
        return DENY  # ignores any role rule
    if guild_id is None:
        return DEFAULT
    ids = effective_roles(guild_id, qualified_name)
    if ids is not None:
        held = {r.id for r in getattr(user, "roles", [])}
        return ALLOW if held & set(ids) else DENY
    if normalize(qualified_name).split(" ", 1)[0] in OWNER_ONLY_DEFAULT:
        return DENY
    return DEFAULT


def all_command_names(tree: app_commands.CommandTree) -> list[str]:
    """Every command and subcommand name, e.g. ["antiraid", "antiraid config", "dm", ...]."""
    names = []

    def walk(cmd):
        names.append(cmd.qualified_name)
        if isinstance(cmd, app_commands.Group):
            for child in cmd.commands:
                walk(child)

    for cmd in tree.get_commands():
        walk(cmd)
    return sorted(names)


# ---------------- slash-command gate ----------------

class GatedTree(app_commands.CommandTree):
    """Command tree that applies the rules above before any slash command handler runs."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        cmd = interaction.command
        if cmd is None or interaction.guild is None:
            return True
        decision = evaluate(interaction.user, interaction.guild.id, cmd.qualified_name)
        if decision == DENY:
            await interaction.response.send_message(NO_PERMS_MESSAGE, ephemeral=True)
            return False
        if isinstance(interaction.user, discord.Member):
            wait = ratelimit.check(interaction.user)  # normal members only; staff are exempt
            if wait is not None:
                await interaction.response.send_message(ratelimit.notice(wait), ephemeral=True)
                return False
        if decision == ALLOW:
            set_granted(True)  # lives only for this interaction's task
        return True

    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CheckFailure):
            return  # we already replied in interaction_check
        await super().on_error(interaction, error)


# Commands that were merged or renamed: rules an owner saved for the old name move to the new one.
# None = the command no longer exists in a form a role rule makes sense for, so the old rule is dropped.
_RENAMED = {
    "purge": "channel purge", "lock": "channel lock", "unlock": "channel lock", "cooldown": "channel slowmode",
    "say-embed": "embed", "dm-embed": "embed", "untimeout": "timeout", "unmute": "mute",
    "setrules": None, "rules": None, "antiraid-scamword add": "antiraid word", "antiraid-scamword remove": "antiraid word",
    "antiraid-scamword list": "antiraid word", "ticketpanel": "panel", "application-panel": "applications panel",
    "debug-ping": "debug", "bot-info": "debug", "requestcmd": "customcmd request",
    # commands merged to cut the command list down (rules on the old names move to the merged command)
    "applications make": "applications setup", "applications edit": "applications setup",
    "applications delete": "applications setup", "applications customize": "applications panel",
    "panel post": "panel", "panel customize": "panel",
    "ticket add": "ticket member", "ticket remove": "ticket member",
    "modmail unclaim": "modmail claim", "modmail unblock": "modmail block", "modmail blocklist": "modmail block",
    "antiraid status": "antiraid config", "antiraid word-add": "antiraid word", "antiraid word-remove": "antiraid word",
    "antiraid word-list": "antiraid word", "honeypot config": "honeypot", "honeypot status": "honeypot",
    "customcmd add": "customcmd set", "customcmd edit": "customcmd set", "customcmd remove": "customcmd set",
    "channel unlock": "channel lock", "role add": "role", "role remove": "role",
    "welcome channel": "welcome", "welcome style": "welcome", "welcome message": "welcome", "welcome image": "welcome",
    "status add": "status", "status remove": "status",
}


def migrate_renamed_rules():
    """Run once at startup. Never overwrites a rule that already exists on the new name."""
    conn = db.get_conn()
    try:
        rows = conn.execute("SELECT guild_id, key, value FROM settings WHERE key LIKE ?", (_PREFIX + "%",)).fetchall()
        existing = {(r["guild_id"], r["key"]) for r in rows}
        for r in rows:
            old = r["key"][len(_PREFIX):]
            if old not in _RENAMED:
                continue
            new = _RENAMED[old]
            if new is not None and (r["guild_id"], _PREFIX + new) not in existing:
                conn.execute("INSERT INTO settings (guild_id, key, value) VALUES (?, ?, ?) "
                             "ON CONFLICT(guild_id, key) DO NOTHING", (r["guild_id"], _PREFIX + new, r["value"]))
                existing.add((r["guild_id"], _PREFIX + new))
            conn.execute("DELETE FROM settings WHERE guild_id=? AND key=?", (r["guild_id"], r["key"]))
        conn.commit()
    finally:
        conn.close()
