import os
import sqlite3
import time

# Keep in sync with daemon/Sources/Store.swift (both sides run CREATE IF NOT EXISTS so either can start first).
SCHEMA = """
CREATE TABLE IF NOT EXISTS segments (
  id INTEGER PRIMARY KEY,
  start REAL NOT NULL,
  end REAL NOT NULL,
  kind TEXT NOT NULL DEFAULT 'active',
  app TEXT,
  bundle TEXT,
  title TEXT,
  url TEXT,
  domain TEXT,
  switch_kind TEXT
);
CREATE INDEX IF NOT EXISTS idx_segments_start ON segments(start);
CREATE INDEX IF NOT EXISTS idx_segments_end ON segments(end);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS projects (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  color INTEGER NOT NULL DEFAULT 0,
  category TEXT NOT NULL DEFAULT 'productive',
  archived INTEGER NOT NULL DEFAULT 0,
  created_at REAL,
  estimate_hours REAL,
  done INTEGER NOT NULL DEFAULT 0,
  completed_at REAL
);
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  done INTEGER NOT NULL DEFAULT 0,
  created_at REAL,
  estimate_hours REAL,
  completed_at REAL
);
CREATE TABLE IF NOT EXISTS rules (
  id INTEGER PRIMARY KEY,
  field TEXT NOT NULL,
  pattern TEXT NOT NULL,
  is_regex INTEGER NOT NULL DEFAULT 0,
  project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
  task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
  category TEXT,
  priority INTEGER NOT NULL DEFAULT 0,
  created_at REAL
);
CREATE TABLE IF NOT EXISTS manual_entries (
  id INTEGER PRIMARY KEY,
  start REAL NOT NULL,
  end REAL NOT NULL,
  title TEXT NOT NULL,
  project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
  task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
  category TEXT,
  note TEXT NOT NULL DEFAULT '',
  created_at REAL
);
CREATE TABLE IF NOT EXISTS plan_blocks (
  id INTEGER PRIMARY KEY,
  day TEXT NOT NULL,
  start REAL NOT NULL,
  end REAL NOT NULL,
  project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
  task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
  note TEXT NOT NULL DEFAULT '',
  created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_plan_blocks_day ON plan_blocks(day);
CREATE TABLE IF NOT EXISTS focus_sessions (
  id INTEGER PRIMARY KEY,
  mode TEXT NOT NULL,               -- blacklist | distracting | whitelist
  start REAL NOT NULL,
  end REAL NOT NULL,                -- planned end (extended in place)
  locked INTEGER NOT NULL DEFAULT 0,
  stopped_at REAL,                  -- set when stopped early or when the session was closed
  created_at REAL
);
CREATE TABLE IF NOT EXISTS focus_sites (
  id INTEGER PRIMARY KEY,
  list TEXT NOT NULL,               -- block | allow | app
  pattern TEXT NOT NULL,
  created_at REAL
);
CREATE TABLE IF NOT EXISTS reflections (
  day TEXT PRIMARY KEY,             -- YYYY-MM-DD
  text TEXT NOT NULL DEFAULT '',
  rating INTEGER,                   -- 1..5, optional
  created_at REAL,
  updated_at REAL
);
CREATE TABLE IF NOT EXISTS assignments (
  id INTEGER PRIMARY KEY,
  start REAL NOT NULL,
  end REAL NOT NULL,
  bundle TEXT,
  project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
  task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
  category TEXT,
  note TEXT NOT NULL DEFAULT '',
  created_at REAL
);
"""

DEFAULT_SETTINGS = {
    "idle_threshold_seconds": "120",   # no input for this long => idle
    "cs_max_seconds": "10",            # a segment shorter than this can be part of a context-switching run
    "cs_min_count": "4",               # ... and the run needs at least this many segments
    "cs_min_distinct": "3",            # ... across at least this many distinct windows/tabs
    "session_gap_seconds": "120",      # same-app sessions separated by less than this are merged
    "min_idle_gap_minutes": "5",       # idle gaps at least this long are offered for "log time away"
    "dashboard_port": "7898",
    "backup_dir": "",                  # e.g. ~/Library/Mobile Documents/com~apple~CloudDocs/Tracker
    "paused": "0",
    "focus_proxy_port": "7897",        # local port of the blocking proxy that blocked requests are sent to
    "always_allow_windows": "[]",      # JSON: [{"start":"19:00","end":"23:00","days":[0,1,2,3,4,5,6]}] hours when the always list is NOT blocked
    "notifications": "1",              # macOS notifications on/off
    "plan_notify_minutes": "5",        # notify this many minutes before a planned block starts (0 = at start, empty = off)
    "reflection_reminder_time": "18:00",  # daily reminder to write the reflection (HH:MM, empty = off)
}

CATEGORIES = ("productive", "neutral", "distracting")

# Columns added after the first release; applied to existing databases on connect.
MIGRATIONS = [
    ("projects", "estimate_hours", "REAL"),
    ("projects", "done", "INTEGER NOT NULL DEFAULT 0"),
    ("projects", "completed_at", "REAL"),
    ("tasks", "estimate_hours", "REAL"),
    ("tasks", "completed_at", "REAL"),
    ("rules", "origin", "TEXT NOT NULL DEFAULT 'manual'"),   # 'auto' = created from an assignment ("remember this window")
]


def _migrate(conn):
    for table, col, decl in MIGRATIONS:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError as e:
                # Two connections can race to add the same column (e.g. the dashboard's parallel first requests).
                if "duplicate column" not in str(e).lower():
                    raise


def data_dir():
    d = os.environ.get("TRACKER_DATA_DIR")
    return os.path.expanduser(d) if d else os.path.expanduser("~/Library/Application Support/Tracker")


def db_path():
    p = os.environ.get("TRACKER_DB")
    return os.path.expanduser(p) if p else os.path.join(data_dir(), "tracker.db")


def connect(path=None):
    path = path or db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=5, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    for k, v in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
    conn.commit()
    return conn


def rows(conn, sql, params=()):
    return [dict(r) for r in conn.execute(sql, params)]


def one(conn, sql, params=()):
    r = conn.execute(sql, params).fetchone()
    return dict(r) if r else None


def get_settings(conn):
    s = dict(DEFAULT_SETTINGS)
    s.update({r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")})
    return s


def set_setting(conn, key, value):
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
    conn.commit()


def now():
    return time.time()
