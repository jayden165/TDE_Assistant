# TDE Assistant

A single-server Discord bot with: honeypot, anti-raid, moderation (and logs), channel utilities,
welcomer, rules, tickets, applications and diagnostics.

## Run

```
pip install -r requirements.txt
cp .env.example .env   # fill in BOT_TOKEN and the IDs
python bot.py
```

Set `GUILD_ID` so slash commands sync to your server instantly.

## Commands

- **Honeypot:** `/honeypot config`, `/honeypot status`
- **Anti-raid:** `/antiraid config|status`, `/antiraid-scamword add|remove|list`
- **Moderation:** `/moderation-panel`, `/role add|remove`, `/say`, `/say-embed`, `/dm`, `/dm-embed`, `/infractions`; bans, kicks, timeouts and mutes are logged to the mod log channel
- **Channel utilities:** `/lock`, `/unlock`, `/cooldown`, `/purge`
- **Welcomer:** `/welcome channel|style|message|image` (image: upload a file or give a link; it's shown in the welcome message)
- **Rules:** `/rules`, `/setrules`
- **Tickets:** `/ticketpanel` (one "Open a Ticket" button; transcripts on close)
- **Applications:** `/application-panel`, `/applications ...`
- **Diagnostics:** `/debug`, `/debug-ping`, `/ping`, `/bot-info`
- **Help:** `/help` lists the commands you're allowed to use
- **Setup:** `/set` (only user 1560257342287777813): set the server ID, staff/admin/mute roles, every log, ticket and
  application channel, the ticket category and the rules link from Discord. Values are saved to `.env` and applied
  immediately. `/set` with no arguments lists everything and flags IDs that don't resolve.
- **Owners:** `/owner action:Add|Remove|List user:` - everyone on the owner list bypasses every
  permission check. Only the root owner (hardcoded in `permissions.py`) can add or remove owners.
  `!sync` re-syncs slash commands (owners only).
- **Command permissions:** `/commandperms action:Allow role|Remove role|List|Reset command: role:` (owners). Pick a command
  (or a subcommand like `role add`) and the roles allowed to use it. Once a command has roles, only owners and those roles
  can use it - this replaces its built-in admin/staff requirement, so it can open a command to a new rank or lock it to a
  smaller one. A rule on a group (`role`) covers its subcommands; a rule on a subcommand wins. `Reset` restores the default.
  `/say`, `/say-embed`, `/dm` and `/dm-embed` are **owner only by default** (even admins are blocked) until an owner allows
  a role. Applies to text-prefix commands and `/help` too.
- **Text prefixes:** `/prefix action:Add|Remove|List prefix:` (owners). Any prefix works, e.g. `s!`, `TDE!`, `DE?`.
  Then `s! rules`, `TDE! purge 20`, `DE? welcome channel #welcome` run the same commands as the slash versions,
  with the same permission checks. Private ("only you can see") replies go to your DMs. Commands that open a
  pop-up form (`/setrules`, `/say-embed`, `/dm-embed`, `/welcome message`, `/applications create`) need the slash version.

## Notes

- Channels and roles are configured in `.env`; `/debug` shows what resolves.
- Data from removed features is deleted automatically on startup (`database._purge_legacy_data`).
