# TDE Assistant

A single-server Discord bot with: honeypot, anti-raid, moderation (and logs), channel utilities,
welcomer, rules, tickets, applications and diagnostics.

```

## Commands at a glance

| Area | Commands |
|---|---|
| Everyone | `/help`, `/rules`, `/ping` |
| Moderation (staff) | `/ban`, `/unban`, `/kick`, `/timeout` (`off` ends it), `/mute` (run again to unmute), `/warn`, `/infractions`, `/moderation-panel`, `/role` (`remove:True` takes it away), `/channel lock\|slowmode\|purge` (`lock time:off` unlocks) |
| Support | `/panel` (support dropdown), `/applications setup\|list\|panel`, `/support-mode`, `/modmail ...` |
| Messages | `/say` (owner; add `user:` to DM), `/embed` (admin popup; add `user:` to DM), `/insmp`, `/customcmd set\|list\|request` |
| Server | `/welcome`, `/rules edit:True` |
| Security | `/honeypot`, `/antiraid config\|word` |
| Bot / owners | `/debug`, `/log-aio`, `/setchannel`, `/set`, `/prefix`, `/owner`, `/commandperms` |

**Rate limit:** normal members can run 5 commands per 30 seconds (slash or text, including custom commands); staff, admins and owners are never limited.
Change it with `MEMBER_COMMAND_LIMIT` / `MEMBER_COMMAND_WINDOW` in `.env` (limit `0` = off). Short text forms still work: `s! purge 20 spam`, `s! lock 1h reason`, `s! slowmode 10s`.

### Details

- **Honeypot:** `/honeypot` (change settings with the options; run it with none to see the current setup)
- **Anti-raid:** `/antiraid config` (no options = show the current setup), and the scam keyword list: `/antiraid word` (give a word to add it, `remove:True` to delete it, no word to list them)
- **Moderation:** `/moderation-panel member:` (admins) opens a private panel for one member with Warn / Timeout / Mute-Unmute / Kick / Ban / View Logs buttons and a status
  line (timed out, muted, past actions). Each button asks for a reason and runs the same code as `/warn`, `/timeout`, `/mute`, `/kick`, `/ban` - same hierarchy checks, DM to the member,
  infraction record and staff action log. `/role` (add, or `remove:True`), `/infractions`;
- **Prefix moderation:** `ban`, `unban`, `kick`, `timeout` (use `off` instead of a duration to end it), `mute` (run it again to unmute), `warn` - also available as slash commands. With a prefix:
  `s! ban <user id or @mention> <reason>`, `s! kick 123456789012345678 being rude`, `s! timeout 123456789012345678 1h spamming`
  (durations: `10m`, `2h`, `1d`, `1h30m`, bare number = minutes, max 28d), `s! warn <user> <reason>`. Bans/unbans work on users who left.
  Needs Ban / Kick / Moderate Members (owners always; `/commandperms` can open them to roles). Can't target yourself, the server owner,
  bot owners or anyone at/above your top role. The user is DM'd the reason; actions land in `/infractions` and the mod log.
  Replies are always embeds (prefix commands reply to your message; errors and bad usage are red embeds). Every use - done, refused or
  failed - is logged to the Staff Action Log channel (falls back to the unified log channel) and the console.
- **/log-aio:** (admins) `/log-aio layout:Separate channels|One channel for everything` creates a private, staff-only "📋 Staff Logs" category with a channel
  for every log type (general, mod, staff actions, anti-raid, honeypot, applications + accepted/denied, ticket transcripts) - or one `staff-logs` channel - and points every
  log setting at it. Staff/admin roles (set with `/set`) get read-only access; @everyone can't see it. Re-running reuses the channels. Needs Manage Channels + Manage Roles.
- **Support mode (tickets / modmail / both):** `/support-mode` (admins) switches how members get support. **Tickets** = the panel dropdown opens a private channel;
  **Modmail** = members DM the bot; **Both** (the default) = either. Switching only affects new conversations (open tickets/threads keep working), and `/panel` posts a panel
  that is a **dropdown** (`/panel`; make it yours with `/panel customize:True`) that looks like a "SUPPORT TICKETS" card: ℹ️ General, ❗ Report, ⚠️ Events and 👑 Admin tickets (when tickets are on; Admin tickets are only visible to the admin role) plus 🎬 *Video Suggestion* (when modmail is on - rename it with `/support-mode modmail_label:...`). Each member can have **one open ticket at a time**. Tickets follow the Ticket Tool pattern: channels are numbered (`general-0001`), the opening message has **Close · Claim · Unclaim · Close Request**, and Close asks "are you sure?" first (the ticket's owner or staff can close). Closing saves an **HTML transcript** to the transcript channel and DMs it to the owner, removes the owner's access and posts **Transcript · Open · Delete**: *Open* brings the ticket back, *Delete* removes the channel after 5 seconds. *Close Request* is for staff - it asks the owner to close (Close / Keep open). `/ticket close [reason] | member (add, or remove:True) | rename | transcript` manage the ticket you're in.
  Picking Video Suggestion opens a form (subject + details, with a minimum length so it can't be vague); a first DM that's too short gets "tell me a bit more" instead of opening a thread; the text goes to staff in a private modmail thread and they reply in the member's DMs. Posted panels update themselves when you switch.
- **Modmail:** DM the bot -> "contact staff?" prompt -> a private channel in the modmail category (falls back to the ticket category) with an info card (account age,
  join date, roles, past threads, infractions) and Claim / Unclaim / Anonymous / Close buttons. Staff just type in the channel and it's DM'd to the member as an embed;
  the member's DMs show up for staff. Attachments are copied, edits sync, ✅ = delivered. Start with `//` for a private staff note. Replies can be anonymous.
  Closing (button, `/modmail close`, or the member's own Close button) saves a transcript to the modmail log channel (falls back to the transcript channel), DMs the
  member and deletes the channel; idle threads auto-close (default 7 days, `/support-mode autoclose_days`, 0 = never). `/modmail contact|block|history` (`block` again unblocks; `block` with no member shows the blocklist; `claim` again releases your claim)
  for the rest. Needs Manage Channels; set the staff role with `/set`. Members mid-application aren't treated as modmail.
- **Recordings:** `/recording-post` (staff) opens a form - name, when, description, max actors - and posts an embed **in the channel you run it in** with a **Join as an actor** button and a live count (`3/10 needed actors`).
  Pressing it asks for a Minecraft username, whitelists it over **RCON** (`wl <username>`), gives the actor role and replies "Thank you for joining this recording!"; a name the server rejects is reported and nothing
  is added. At the maximum the button is removed and the post says the needed actors were chosen. `/recording-end` (bot owners) takes everyone who signed up off the whitelist (`whitelist remove`; people who were
  already whitelisted are left alone) and removes the role. `/recording-ban user` (staff) stops someone ever signing up (reason `lift` undoes it). Set `RCON_HOST`, `RCON_PORT`, `RCON_PASSWORD`
  (and optionally `RCON_ADD_COMMAND` / `RCON_REMOVE_COMMAND`, `RECORDING_ACTOR_ROLE_ID`) in `.env`; enable RCON on the server (`enable-rcon=true`, `rcon.port`, `rcon.password` in `server.properties`).
- **Global ban:** a ban in **any server the bot is in** is mirrored to every other server (reason `[Global ban] Banned in <server>: <original reason>`; servers that already banned them are left alone).
  An unban lifts the global bans the bot made elsewhere - but not a ban another server placed on its own, unless `GLOBAL_UNBAN_ALL=true`. The bot and bot owners are never globally banned, and the bans the bot
  makes can't loop. A summary (applied / skipped / failed, e.g. missing Ban Members) goes to the origin server's mod log and a notice to each affected server's. Needs the bot in each server with **Ban Members**;
  turn it off with `GLOBAL_BAN_ENABLED=false` in `.env`.
- **Bot status:** `/status activity:Watching text:over the server`, `/status text:... remove:True`, or `/status` alone to list them (bot owners only; also `M! status watching over the server`). Add as many as you like - with one it
  stays, with several they **rotate every minute**. Activities: Playing, Watching, Listening to, Competing in. `remove` autocompletes from the list; every reply shows the current list. Saved across restarts.
- **YouTube updates:** `/upload channel set channel:#updates [ping:@role]` (admins) posts a new **video, Short or community post** from **The Daily Emerald** (https://www.youtube.com/@thedailyemeraldmc) in that channel - an embed with the
  thumbnail / text and a Watch button, pinging the role if you gave one. `/upload channel remove` stops it; `/upload check` tests the connection and posts nothing. The bot checks every 5 minutes (`YOUTUBE_POLL_MINUTES`) using the channel's
  public feed - **no API key needed** - and only announces *new* uploads (the first run just records what's already there; anything older than 2 days is never posted). Videos and Shorts come from YouTube's feed and are reliable;
  community posts have no official feed, so they're read from the Posts page and could stop working if YouTube changes it (`/upload check` shows whether they're readable). Also `M! upload channel set #updates`.
- **YouTube notifications:** `/upload channel set [channel]` (admins; also `M! upload channel set #uploads`) posts an embed with a **Watch** button whenever **The Daily Emerald** uploads a **video**, a **Short**, or makes a
  **community post**. `/upload channel remove` stops it, `/upload ping [role]` pings a role (empty = nobody), `/upload status` shows the setup and any problems, `/upload check` checks right now. The bot checks every 5 minutes
  (`YOUTUBE_POLL_MINUTES`), needs no API key (RSS feed + the public Posts tab), only remembers what's already there on the first check (old uploads aren't announced) and posts at most 5 items per check. Community posts have no
  official API, so that part is best-effort. The watched channel is `YOUTUBE_CHANNEL_ID` / `_HANDLE` / `_NAME` in `.env` (default: The Daily Emerald).
- **Restart:** `/restart` (bot owners only; also the text command `!restart`) restarts the bot and re-reads its env file (`tde.env`, or `.env`; set `ENV_FILE` to use another path). It first runs the same startup check as CI and, if the bot wouldn't start, tells you why and does **not** restart. When the bot is back it posts "back online" in the channel you used it in, and the restart is logged to the Staff Action Log. Only values from the env file are re-read; variables set in a host's dashboard can only be changed there.
- **Prefix help:** `s! help` lists every text command you can use (grouped, with usage); `s! help ban` shows one command's options and an example.
- **Custom commands:** `/customcmd set|list` (owners; `set` with no name creates one, with a name edits it, `delete:True` removes it) makes simple text commands from a form - people run them with a prefix (`?ip`, `s! discord`).
  Responses can be plain or an embed and can use `{user}`, `{username}`, `{server}`, `{args}`. They can't run code or reuse a built-in name.
- **/customcmd request:** (owners) for features a custom command can't do - a form (command, what it does, reason, who/options, extra info) that DMs the developer
  (`DEV_USER_ID` in `.env`, default: the setup user) with the requester, server and channel; falls back to the staff action log if DMs are closed.
- **/setchannel:** (any bot owner) `/setchannel` lists every channel setting; `/setchannel setting:Ticket Panel Channel channel:#tickets` changes one
  (ticket panel, transcripts, all log channels, application channels, ticket category); `clear:True` goes back to the `.env` value. Saved per server, applies
  immediately, wins over `.env`; the bot warns if it can't post in the channel. Also works as `s! setchannel ticket-panel #tickets`. Changes are logged.
- **/insmp:** `/insmp` (owners only) opens a form to set a custom message. Anyone typing `<prefix>insmp` (e.g. `?insmp`, add the `?` prefix
  with `/prefix`) gets that message back. @everyone/role pings in it are suppressed. Leave the form blank to clear it.
- **Channel utilities:** `/channel lock|slowmode|purge` (lock/slowmode are admin, purge is staff; `/channel lock time:off` unlocks)
- **Welcomer:** `/welcome` with options `channel`, `style`, `image` (upload) or `url`, `remove_image`, `card`, `details` (`False` hides the member info line / profile fields), `edit_message:True` (opens the text popup); run it with no options to preview the welcome message.
  Every welcome comes with a **welcome card** made fresh for the new member - the dark "Member #N" card with their avatar and name (`welcome_card.py`, drawn on `assets/welcome_card_template.png`).
  `/welcome card:False` turns it off (the server's welcome `image`/`url`, if any, is used instead; also used if a card can't be made). For the exact original look put `Raleway-Bold.ttf` and
  `Raleway-SemiBold.ttf` (free from Google Fonts) in `assets/fonts/`; without them it uses a system font.
- **Rules:** `/rules` shows them; admins use `/rules edit:True` to set or clear them
- **Panels:** `/panel` posts the support dropdown (tickets / modmail) and `/panel customize:True` changes how it looks (title, text, colour, banner, placeholder in a form; `ticket_label`, `modmail_label`, `footer`, `thumbnail`, `show_list`, `reset:True` as options - posted panels update themselves); `/applications panel` posts the applications menu (add `customize:True` to restyle it). Ticket transcripts are saved on close.
- **Applications:** one group, `/applications setup | list | panel` (`setup` with no name creates an application, with a name edits it, `delete:True` removes it; `panel` posts the menu, `panel customize:True` restyles it). `panel` posts an embed with **one button per application**. Pressing a button opens the
  application as a **form** (or, for **Staff** applications and anything set with `dm:True`, sends the questions to the applicant's **DMs** one at a time - reply to each, `cancel` to stop; a form shows 5 questions per page - longer ones continue on the next page - with the question as each field's label), then a **private ticket** is opened with the
  member and the staff team. The ticket's opening message *is* the application (all the answers) with **Claim · Unclaim · Close · Close Request** buttons, plus Accept / Deny: Accept
  gives the application's role(s) - **Staff** applications automatically get the two staff roles too (matched on the name, on top of any role picked with `role:`) - and DMs the applicant (and if the application has an invite - **Voice Actor** applications automatically send the DE content server invite, or set your own with `invite:` on `make`/`edit` - the invite is in the DM and the ticket, with a join button), and a copy goes to the accepted/denied log channels if set. `make` and `edit` open a form with the **name, emoji, description**
  and up to **20 questions** (one per line, max 100 characters each); the accept role and notification channel are pickers on the command (`/applications setup role:@Member channel:#reviews dm:True`;
  on `edit` add `clear_role` / `clear_channel`). `customize` changes the panel's look (title, text, colour, banner, button colour; `footer`, `thumbnail`, `show_list`, `reset:True`).
  The one-open-ticket-per-member limit applies, so someone can't apply while they already have a ticket open. Posted panels update themselves.
- **Diagnostics:** `/debug` (bot info, cogs, channels, roles, permissions), `/ping`
- **Help:** `/help` lists the commands you're allowed to use
- **Setup:** `/set` (only user 1560257342287777813): set the server ID, staff/admin/mute roles, every log, ticket and
  application channel, the ticket category and the rules link from Discord. Values are saved to `.env` and applied
  immediately. `/set` with no arguments lists everything and flags IDs that don't resolve.
- **Owners:** `/owner action:Add|Remove|List user:` - everyone on the owner list bypasses every
  permission check. Only the root owner (hardcoded in `permissions.py`) can add or remove owners.
  `!sync` re-syncs slash commands (owners only).
- **Command permissions:** `/commandperms action:Allow role|Remove role|List|Reset command: role:` (owners). Pick a command
  (or a subcommand like `role add` or `channel purge`) and the roles allowed to use it. Once a command has roles, only owners and those roles
  can use it - this replaces its built-in admin/staff requirement, so it can open a command to a new rank or lock it to a
  smaller one. A rule on a group (`role`) covers its subcommands; a rule on a subcommand wins. `Reset` restores the default.
  `/embed` is **owner only by default** (even admins are blocked) until an owner allows a role.
  `/say`, `/insmp`, `/customcmd`, `/setchannel`, `/status` and `/recording-end` are **owner only, always** (bot owners from `/owner`) and can't be given to roles. They send the
  message exactly as typed (no "sent by" line) and log who used them to the staff action log channel. Applies to text-prefix commands and `/help` too.
- **Text prefixes:** `/prefix action:Add|Remove|List prefix:` (owners). Any prefix works, e.g. `s!`, `TDE!`, `DE?`.
  Then `s! rules`, `TDE! purge 20 spam`, `DE? welcome #welcome` run the same commands as the slash versions,
  with the same permission checks. Private ("only you can see") replies go to your DMs. Commands that open a
  pop-up form (`/rules edit`, `/embed`, `/welcome edit_message:True`, `/applications setup`, `/customcmd set`) need the slash version.

## Notes

- Made By the staff team
- This is a private bot - Please reach out if you wish to use any of the code
- Don't pass this off as your own work please. The team worked hard on it 


