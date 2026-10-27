import sqlite3
import time
import os
import json

# Lets you point the DB at a mounted persistent volume (e.g. Fly.io's /data) via
# an env var. Falls back to sitting next to this file for local runs.
DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "bot.db"))


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel_id INTEGER NOT NULL UNIQUE,
        guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        claimed_by INTEGER,
        status TEXT NOT NULL DEFAULT 'open',   -- open, closed
        created_at REAL NOT NULL
    )
    """)
    c.execute("""
    CREATE TABLE IF NOT EXISTS infractions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        type TEXT NOT NULL,      -- strike, warning, demote
        reason TEXT NOT NULL,
        staff_id INTEGER NOT NULL,
        created_at REAL NOT NULL
    )
    """)
    c.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        guild_id INTEGER NOT NULL,
        key TEXT NOT NULL,
        value TEXT,
        PRIMARY KEY (guild_id, key)
    )
    """)
    c.execute("""
    CREATE TABLE IF NOT EXISTS applications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        app_type TEXT NOT NULL,
        answers TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at REAL NOT NULL
    )
    """)
    c.execute("""
    CREATE TABLE IF NOT EXISTS modmail_threads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'open',      -- open, closed
        claimed_by INTEGER,
        anonymous INTEGER NOT NULL DEFAULT 0,
        opened_by INTEGER,                        -- staff member, if staff started it
        closed_by INTEGER,
        close_reason TEXT,
        created_at REAL NOT NULL,
        closed_at REAL
    )
    """)
    c.execute("""
    CREATE TABLE IF NOT EXISTS modmail_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        thread_id INTEGER NOT NULL,
        author_id INTEGER NOT NULL,
        author_name TEXT NOT NULL,
        source TEXT NOT NULL,                     -- user, staff, note, system
        content TEXT NOT NULL,
        attachments TEXT,                         -- JSON list of "filename|url"
        channel_msg_id INTEGER,
        dm_msg_id INTEGER,
        created_at REAL NOT NULL
    )
    """)
    c.execute("""
    CREATE TABLE IF NOT EXISTS modmail_blocks (
        guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        reason TEXT,
        staff_id INTEGER NOT NULL,
        created_at REAL NOT NULL,
        PRIMARY KEY (guild_id, user_id)
    )
    """)
    c.execute("""
    CREATE TABLE IF NOT EXISTS recordings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        channel_id INTEGER,
        message_id INTEGER,
        title TEXT NOT NULL,
        when_text TEXT NOT NULL,
        when_ts INTEGER,
        description TEXT,
        max_actors INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'open',
        created_by INTEGER,
        created_at REAL NOT NULL
    )
    """)
    c.execute("""
    CREATE TABLE IF NOT EXISTS recording_signups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        recording_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        mc_username TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        already_wl INTEGER NOT NULL DEFAULT 0,
        cleaned INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        UNIQUE (recording_id, user_id)
    )
    """)
    c.execute("""
    CREATE TABLE IF NOT EXISTS recording_bans (
        guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        reason TEXT,
        banned_by INTEGER,
        created_at REAL NOT NULL,
        PRIMARY KEY (guild_id, user_id)
    )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_mm_msgs_thread ON modmail_messages (thread_id)")
    _purge_legacy_data(c)
    conn.commit()
    conn.close()


# ---------------- legacy data cleanup ----------------

# Tables and settings left behind by features that no longer exist (blacklist, ticket
# categories, feature toggles, kill switch, global updates, premium, dashboard, status rotation,
# /setup-only overrides). Runs on every start; it's a no-op once the data is gone.
_LEGACY_TABLES = ("blacklist", "dashboard_blocklist")
_LEGACY_SETTING_KEYS = (
    "bot_stopped", "premium", "custom_ticket_types", "join_message:extra_text",
    "status_cycle", "status_cycle_enabled", "bot:desired_status",
    "cfg:updates-channel", "cfg:staff-updates-channel",
)
_LEGACY_SETTING_PREFIXES = ("tickettype-disabled:", "feature:", "cfg:staff-promotions", "cfg:staff-infractions",
                            "cfg:support-channel", "cfg:base-member-role", "bot:joined_at")


def _purge_legacy_data(c):
    for table in _LEGACY_TABLES:
        c.execute(f"DROP TABLE IF EXISTS {table}")
    marks = ",".join("?" for _ in _LEGACY_SETTING_KEYS)
    c.execute(f"DELETE FROM settings WHERE key IN ({marks})", _LEGACY_SETTING_KEYS)
    for prefix in _LEGACY_SETTING_PREFIXES:
        c.execute("DELETE FROM settings WHERE key LIKE ?", (f"{prefix}%",))
    cols = [row[1] for row in c.execute("PRAGMA table_info(tickets)").fetchall()]
    if "ticket_type" not in cols:  # which panel option opened it (general / report / events / admin)
        c.execute("ALTER TABLE tickets ADD COLUMN ticket_type TEXT")
    # Tickets no longer have a category.
    if "category" in cols:
        try:
            c.execute("ALTER TABLE tickets DROP COLUMN category")
        except sqlite3.OperationalError:
            pass  # SQLite older than 3.35 can't drop columns; the extra column is harmless but unused


# ---------------- tickets ----------------

def create_ticket(channel_id, guild_id, user_id, ticket_type=None):
    conn = get_conn(); c = conn.cursor()
    c.execute("INSERT INTO tickets (channel_id, guild_id, user_id, created_at, ticket_type) VALUES (?, ?, ?, ?, ?)",
              (channel_id, guild_id, user_id, time.time(), ticket_type))
    conn.commit(); conn.close()


def count_tickets(guild_id):
    """How many tickets this server has ever had - used for the ticket-0001 style numbering."""
    conn = get_conn(); c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM tickets WHERE guild_id=?", (guild_id,))
    n = c.fetchone()[0]; conn.close()
    return n


def reopen_ticket(channel_id):
    conn = get_conn(); c = conn.cursor()
    c.execute("UPDATE tickets SET status='open' WHERE channel_id=?", (channel_id,))
    conn.commit(); conn.close()


def get_open_tickets_for_user(guild_id, user_id):
    conn = get_conn(); c = conn.cursor()
    c.execute("SELECT * FROM tickets WHERE guild_id=? AND user_id=? AND status='open'", (guild_id, user_id))
    rows = c.fetchall(); conn.close()
    return rows


def get_ticket(channel_id):
    conn = get_conn(); c = conn.cursor()
    c.execute("SELECT * FROM tickets WHERE channel_id=?", (channel_id,))
    row = c.fetchone(); conn.close()
    return row


def claim_ticket(channel_id, staff_id):
    conn = get_conn(); c = conn.cursor()
    c.execute("UPDATE tickets SET claimed_by=? WHERE channel_id=?", (staff_id, channel_id))
    conn.commit(); conn.close()


def unclaim_ticket(channel_id):
    conn = get_conn(); c = conn.cursor()
    c.execute("UPDATE tickets SET claimed_by=NULL WHERE channel_id=?", (channel_id,))
    conn.commit(); conn.close()


def close_ticket(channel_id):
    conn = get_conn(); c = conn.cursor()
    c.execute("UPDATE tickets SET status='closed' WHERE channel_id=?", (channel_id,))
    conn.commit(); conn.close()


# ---------------- infractions (strikes / warnings / demotions) ----------------

def add_infraction(guild_id, user_id, type_, reason, staff_id):
    conn = get_conn(); c = conn.cursor()
    c.execute("INSERT INTO infractions (guild_id, user_id, type, reason, staff_id, created_at) "
              "VALUES (?, ?, ?, ?, ?, ?)", (guild_id, user_id, type_, reason, staff_id, time.time()))
    conn.commit(); conn.close()


def get_infractions(guild_id, user_id, limit=25):
    conn = get_conn(); c = conn.cursor()
    c.execute("SELECT * FROM infractions WHERE guild_id=? AND user_id=? ORDER BY created_at DESC LIMIT ?",
              (guild_id, user_id, limit))
    rows = c.fetchall(); conn.close()
    return rows


def count_infractions(guild_id, user_id, type_=None):
    conn = get_conn(); c = conn.cursor()
    if type_:
        c.execute("SELECT COUNT(*) as n FROM infractions WHERE guild_id=? AND user_id=? AND type=?",
                  (guild_id, user_id, type_))
    else:
        c.execute("SELECT COUNT(*) as n FROM infractions WHERE guild_id=? AND user_id=?", (guild_id, user_id))
    row = c.fetchone(); conn.close()
    return row["n"]


# ---------------- settings (runtime-configurable per-guild values) ----------------

def get_setting(guild_id, key, default=None):
    conn = get_conn(); c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE guild_id=? AND key=?", (guild_id, key))
    row = c.fetchone(); conn.close()
    return row["value"] if row else default


def set_setting(guild_id, key, value):
    conn = get_conn(); c = conn.cursor()
    c.execute("INSERT INTO settings (guild_id, key, value) VALUES (?, ?, ?) "
              "ON CONFLICT(guild_id, key) DO UPDATE SET value=excluded.value", (guild_id, key, str(value)))
    conn.commit(); conn.close()


def delete_setting(guild_id, key):
    conn = get_conn(); c = conn.cursor()
    c.execute("DELETE FROM settings WHERE guild_id=? AND key=?", (guild_id, key))
    conn.commit(); conn.close()


def get_settings_by_prefix(guild_id, prefix):
    conn = get_conn(); c = conn.cursor()
    c.execute("SELECT * FROM settings WHERE guild_id=? AND key LIKE ?", (guild_id, f"{prefix}%"))
    rows = c.fetchall(); conn.close()
    return rows


# ---------------- applications ----------------

def create_application(guild_id, user_id, app_type, answers):
    conn = get_conn(); c = conn.cursor()
    c.execute("INSERT INTO applications (guild_id, user_id, app_type, answers, status, created_at) "
              "VALUES (?, ?, ?, ?, 'pending', ?)",
              (guild_id, user_id, app_type, json.dumps(answers), time.time()))
    conn.commit()
    aid = c.lastrowid
    conn.close()
    return aid


def get_application(app_id):
    conn = get_conn(); c = conn.cursor()
    c.execute("SELECT * FROM applications WHERE id=?", (app_id,))
    row = c.fetchone(); conn.close()
    return row


def set_application_status(app_id, status):
    conn = get_conn(); c = conn.cursor()
    c.execute("UPDATE applications SET status=? WHERE id=?", (status, app_id))
    conn.commit(); conn.close()


# ---------------- modmail ----------------

def _fetch(sql, params=(), many=False):
    conn = get_conn(); c = conn.cursor()
    c.execute(sql, params)
    rows = c.fetchall() if many else c.fetchone()
    conn.close()
    return rows


def _run(sql, params=()):
    conn = get_conn(); c = conn.cursor()
    c.execute(sql, params)
    conn.commit()
    last = c.lastrowid
    conn.close()
    return last


def mm_create_thread(guild_id, user_id, channel_id, opened_by=None):
    return _run("INSERT INTO modmail_threads (guild_id, user_id, channel_id, opened_by, created_at) "
                "VALUES (?, ?, ?, ?, ?)", (guild_id, user_id, channel_id, opened_by, time.time()))


def mm_get_thread(thread_id):
    return _fetch("SELECT * FROM modmail_threads WHERE id=?", (thread_id,))


def mm_get_open_by_user(guild_id, user_id):
    return _fetch("SELECT * FROM modmail_threads WHERE guild_id=? AND user_id=? AND status='open' "
                  "ORDER BY id DESC LIMIT 1", (guild_id, user_id))


def mm_get_open_by_channel(channel_id):
    return _fetch("SELECT * FROM modmail_threads WHERE channel_id=? AND status='open'", (channel_id,))


def mm_open_channel_ids():
    return {r["channel_id"] for r in _fetch("SELECT channel_id FROM modmail_threads WHERE status='open'", many=True)}


def mm_count_open(guild_id):
    return _fetch("SELECT COUNT(*) AS n FROM modmail_threads WHERE guild_id=? AND status='open'", (guild_id,))["n"]


def mm_threads_for_user(guild_id, user_id, limit=10):
    return _fetch("SELECT * FROM modmail_threads WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT ?",
                  (guild_id, user_id, limit), many=True)


def mm_count_threads(guild_id, user_id):
    return _fetch("SELECT COUNT(*) AS n FROM modmail_threads WHERE guild_id=? AND user_id=?",
                  (guild_id, user_id))["n"]


_MM_THREAD_COLUMNS = {"claimed_by", "anonymous"}


def mm_update_thread(thread_id, **fields):
    fields = {k: v for k, v in fields.items() if k in _MM_THREAD_COLUMNS}
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    _run(f"UPDATE modmail_threads SET {sets} WHERE id=?", (*fields.values(), thread_id))


def mm_close_thread(thread_id, closed_by, reason):
    _run("UPDATE modmail_threads SET status='closed', closed_by=?, close_reason=?, closed_at=? WHERE id=?",
         (closed_by, reason, time.time(), thread_id))


def mm_add_message(thread_id, author_id, author_name, source, content, attachments=None,
                   channel_msg_id=None, dm_msg_id=None):
    return _run("INSERT INTO modmail_messages (thread_id, author_id, author_name, source, content, attachments, "
                "channel_msg_id, dm_msg_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (thread_id, author_id, author_name, source, content or "", json.dumps(attachments or []),
                 channel_msg_id, dm_msg_id, time.time()))


def mm_messages(thread_id):
    return _fetch("SELECT * FROM modmail_messages WHERE thread_id=? ORDER BY id", (thread_id,), many=True)


def mm_count_messages(thread_id):
    return _fetch("SELECT COUNT(*) AS n FROM modmail_messages WHERE thread_id=?", (thread_id,))["n"]


def mm_find_user_message(dm_msg_id):
    return _fetch("SELECT * FROM modmail_messages WHERE source='user' AND dm_msg_id=?", (dm_msg_id,))


def mm_find_staff_message(channel_msg_id):
    return _fetch("SELECT * FROM modmail_messages WHERE source='staff' AND channel_msg_id=?", (channel_msg_id,))


def mm_update_message_content(message_id, content):
    _run("UPDATE modmail_messages SET content=? WHERE id=?", (content, message_id))


def mm_block(guild_id, user_id, reason, staff_id):
    _run("INSERT INTO modmail_blocks (guild_id, user_id, reason, staff_id, created_at) VALUES (?, ?, ?, ?, ?) "
         "ON CONFLICT(guild_id, user_id) DO UPDATE SET reason=excluded.reason, staff_id=excluded.staff_id, "
         "created_at=excluded.created_at", (guild_id, user_id, reason, staff_id, time.time()))


def mm_unblock(guild_id, user_id) -> bool:
    conn = get_conn(); c = conn.cursor()
    c.execute("DELETE FROM modmail_blocks WHERE guild_id=? AND user_id=?", (guild_id, user_id))
    conn.commit(); removed = c.rowcount > 0; conn.close()
    return removed


def mm_get_block(guild_id, user_id):
    return _fetch("SELECT * FROM modmail_blocks WHERE guild_id=? AND user_id=?", (guild_id, user_id))


def mm_blocks(guild_id):
    return _fetch("SELECT * FROM modmail_blocks WHERE guild_id=? ORDER BY created_at DESC", (guild_id,), many=True)


# ---------------------------------------------------------------------------
# Recordings (/recording-post): a post with a sign-up button, its actors, and the actor ban list
# ---------------------------------------------------------------------------

def rec_create(guild_id, channel_id, title, when_text, when_ts, description, max_actors, created_by):
    conn = get_conn(); c = conn.cursor()
    c.execute("INSERT INTO recordings (guild_id, channel_id, title, when_text, when_ts, description, max_actors, "
              "created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
              (guild_id, channel_id, title, when_text, when_ts, description, max_actors, created_by, time.time()))
    rec_id = c.lastrowid
    conn.commit(); conn.close()
    return rec_id


def rec_get(rec_id):
    return _fetch("SELECT * FROM recordings WHERE id=?", (rec_id,))


def rec_get_by_message(message_id):
    return _fetch("SELECT * FROM recordings WHERE message_id=?", (message_id,))


def rec_set_message(rec_id, channel_id, message_id):
    _run("UPDATE recordings SET channel_id=?, message_id=? WHERE id=?", (channel_id, message_id, rec_id))


def rec_set_status(rec_id, status):
    _run("UPDATE recordings SET status=? WHERE id=?", (status, rec_id))


def rec_delete(rec_id):
    _run("DELETE FROM recording_signups WHERE recording_id=?", (rec_id,))
    _run("DELETE FROM recordings WHERE id=?", (rec_id,))


def rec_unfinished(guild_id):
    return _fetch("SELECT * FROM recordings WHERE guild_id=? AND status!='ended' ORDER BY id DESC", (guild_id,), many=True)


def rec_count(rec_id, joined_only=False):
    """Actors signed up. Includes people mid-signup (their slot is held) unless joined_only."""
    sql = "SELECT COUNT(*) AS n FROM recording_signups WHERE recording_id=?"
    if joined_only:
        sql += " AND status='joined'"
    return _fetch(sql, (rec_id,))["n"]


def rec_add_signup(rec_id, user_id, mc_username):
    """Holds a slot for the user. Returns the signup id, or None if they already have one."""
    conn = get_conn(); c = conn.cursor()
    try:
        c.execute("INSERT INTO recording_signups (recording_id, user_id, mc_username, created_at) VALUES (?, ?, ?, ?)",
                  (rec_id, user_id, mc_username, time.time()))
        conn.commit()
        return c.lastrowid
    except sqlite3.IntegrityError:
        return None
    finally:
        conn.close()


def rec_signup_joined(signup_id, already_wl):
    _run("UPDATE recording_signups SET status='joined', already_wl=? WHERE id=?", (1 if already_wl else 0, signup_id))


def rec_signup_delete(signup_id):
    _run("DELETE FROM recording_signups WHERE id=?", (signup_id,))


def rec_signup_cleaned(signup_id):
    _run("UPDATE recording_signups SET cleaned=1 WHERE id=?", (signup_id,))


def rec_user_signup(rec_id, user_id):
    return _fetch("SELECT * FROM recording_signups WHERE recording_id=? AND user_id=?", (rec_id, user_id))


def rec_username_taken(rec_id, mc_username):
    return _fetch("SELECT 1 FROM recording_signups WHERE recording_id=? AND LOWER(mc_username)=LOWER(?)",
                  (rec_id, mc_username)) is not None


def rec_joined(rec_id):
    return _fetch("SELECT * FROM recording_signups WHERE recording_id=? AND status='joined' ORDER BY id", (rec_id,), many=True)


def rec_clear_pending():
    """Signups that never finished (the bot restarted mid-signup) - free their slots."""
    _run("DELETE FROM recording_signups WHERE status='pending'")


def rec_ban(guild_id, user_id, reason, banned_by):
    _run("INSERT OR REPLACE INTO recording_bans (guild_id, user_id, reason, banned_by, created_at) VALUES (?, ?, ?, ?, ?)",
         (guild_id, user_id, reason, banned_by, time.time()))


def rec_unban(guild_id, user_id):
    conn = get_conn(); c = conn.cursor()
    c.execute("DELETE FROM recording_bans WHERE guild_id=? AND user_id=?", (guild_id, user_id))
    removed = c.rowcount > 0
    conn.commit(); conn.close()
    return removed


def rec_get_ban(guild_id, user_id):
    return _fetch("SELECT * FROM recording_bans WHERE guild_id=? AND user_id=?", (guild_id, user_id))
