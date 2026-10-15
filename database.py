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
    # Tickets no longer have a category.
    cols = [row[1] for row in c.execute("PRAGMA table_info(tickets)").fetchall()]
    if "category" in cols:
        try:
            c.execute("ALTER TABLE tickets DROP COLUMN category")
        except sqlite3.OperationalError:
            pass  # SQLite older than 3.35 can't drop columns; the extra column is harmless but unused


# ---------------- tickets ----------------

def create_ticket(channel_id, guild_id, user_id):
    conn = get_conn(); c = conn.cursor()
    c.execute("INSERT INTO tickets (channel_id, guild_id, user_id, created_at) VALUES (?, ?, ?, ?)",
              (channel_id, guild_id, user_id, time.time()))
    conn.commit(); conn.close()


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
