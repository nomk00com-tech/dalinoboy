"""
SQLite database layer — all queries in one place.

Schema:
    users       — telegram users (admin / regular)
    vehicles    — cars with plate numbers
    trackers    — GPS trackers linked to vehicles
    trips       — active SENT/RMPD trips
    trip_checks — history of GPS checks per tracker per trip
"""

import os
import aiosqlite
import logging
from datetime import datetime

log = logging.getLogger(__name__)

# On Railway the container filesystem is ephemeral — set DB_PATH to a mounted
# volume (e.g. /data/puesc_bot.db) so data survives redeploys/restarts.
DB_PATH = os.getenv("DB_PATH", "puesc_bot.db")


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id          INTEGER PRIMARY KEY,
    name                 TEXT    NOT NULL,
    username             TEXT,
    is_admin             INTEGER NOT NULL DEFAULT 0,
    vehicle_id           INTEGER,   -- driver's assigned vehicle (NULL = none/dispatcher)
    notifications_enabled INTEGER NOT NULL DEFAULT 1,
    created_at           TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Drivers the admin pre-authorises (by telegram_id or @username) before they /start.
CREATE TABLE IF NOT EXISTS allowlist (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER,
    username    TEXT,           -- lowercase, without '@'
    label       TEXT,           -- optional human label
    added_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS vehicles (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL,
    plate_number TEXT    NOT NULL UNIQUE,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS trackers (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    vehicle_id     INTEGER NOT NULL REFERENCES vehicles(id) ON DELETE CASCADE,
    provider       TEXT    NOT NULL,   -- 'Globus' | 'Lontex' | other
    tracker_number TEXT    NOT NULL,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (vehicle_id, provider)
);

CREATE TABLE IF NOT EXISTS trips (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    vehicle_id   INTEGER NOT NULL REFERENCES vehicles(id) ON DELETE CASCADE,
    rmpd_number  TEXT    NOT NULL,                    -- CURRENT (latest) RMPD we drive by
    status       TEXT    NOT NULL DEFAULT 'active',  -- 'active' | 'finished'
    alarm_stage  INTEGER NOT NULL DEFAULT 0,          -- 0 ok | 1 warned (30m) | 2 fine (60m)
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    finished_at  TEXT
);

-- One trip can stack several RMPDs over time. We monitor only the latest one;
-- older ones are kept here as history.
CREATE TABLE IF NOT EXISTS trip_rmpds (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id     INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    rmpd_number TEXT    NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS news_seen (
    article_id  TEXT PRIMARY KEY,
    title       TEXT,
    seen_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS trip_checks (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id            INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    tracker_id         INTEGER NOT NULL REFERENCES trackers(id) ON DELETE CASCADE,
    status             TEXT    NOT NULL,   -- signal_ok | signal_missing | invalid_data | site_error | unknown_response
    last_position_time TEXT,
    latitude           REAL,
    longitude          REAL,
    alarm              INTEGER NOT NULL DEFAULT 0,  -- 1 = alarm active at this check (used for anti-spam)
    raw_message        TEXT,
    created_at         TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""


async def init_db():
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        await db.executescript(SCHEMA)
        await db.execute("PRAGMA journal_mode=WAL")  # better read/write concurrency
        # Migrate older DBs: add columns that may be missing.
        for col, ddl in [("username", "TEXT"), ("vehicle_id", "INTEGER")]:
            try:
                await db.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")
            except Exception:
                pass  # column already exists
        try:
            await db.execute("ALTER TABLE trips ADD COLUMN alarm_stage INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass
        # Backfill RMPD history for trips created before trip_rmpds existed.
        await db.execute(
            "INSERT INTO trip_rmpds (trip_id, rmpd_number) "
            "SELECT id, rmpd_number FROM trips WHERE id NOT IN (SELECT trip_id FROM trip_rmpds)"
        )
        await db.commit()
    log.info("Database initialised at %s", DB_PATH)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

async def upsert_user(telegram_id: int, name: str, is_admin: bool = False, username: str | None = None):
    uname = username.lower().lstrip("@") if username else None
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        await db.execute(
            """INSERT INTO users (telegram_id, name, is_admin, username)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(telegram_id) DO UPDATE SET
                   name = excluded.name,
                   is_admin = excluded.is_admin,
                   username = COALESCE(excluded.username, users.username)""",
            (telegram_id, name, int(is_admin), uname),
        )
        await db.commit()


async def set_user_vehicle(telegram_id: int, vehicle_id: int | None):
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        await db.execute(
            "UPDATE users SET vehicle_id = ?, notifications_enabled = 1 WHERE telegram_id = ?",
            (vehicle_id, telegram_id),
        )
        await db.commit()


async def get_drivers_for_vehicle(vehicle_id: int) -> list[dict]:
    """Users assigned to this vehicle who have notifications on."""
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE vehicle_id = ? AND notifications_enabled = 1",
            (vehicle_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_user(telegram_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_all_users() -> list[dict]:
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users ORDER BY name") as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_notification_recipients() -> list[dict]:
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE notifications_enabled = 1"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def set_user_admin(telegram_id: int, is_admin: bool):
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        await db.execute(
            "UPDATE users SET is_admin = ? WHERE telegram_id = ?",
            (int(is_admin), telegram_id),
        )
        await db.commit()


async def set_notifications(telegram_id: int, enabled: bool):
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        await db.execute(
            "UPDATE users SET notifications_enabled = ? WHERE telegram_id = ?",
            (int(enabled), telegram_id),
        )
        await db.commit()


async def delete_user(telegram_id: int):
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        await db.execute("DELETE FROM users WHERE telegram_id = ?", (telegram_id,))
        await db.commit()


# ---------------------------------------------------------------------------
# Allowlist (pre-authorised drivers)
# ---------------------------------------------------------------------------

async def add_allow(telegram_id: int | None = None, username: str | None = None, label: str | None = None) -> int:
    uname = username.lower().lstrip("@") if username else None
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        cur = await db.execute(
            "INSERT INTO allowlist (telegram_id, username, label) VALUES (?, ?, ?)",
            (telegram_id, uname, label),
        )
        await db.commit()
        return cur.lastrowid


async def get_allowlist() -> list[dict]:
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM allowlist ORDER BY added_at DESC") as cur:
            return [dict(r) for r in await cur.fetchall()]


async def remove_allow(entry_id: int):
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        await db.execute("DELETE FROM allowlist WHERE id = ?", (entry_id,))
        await db.commit()


async def is_allowed(telegram_id: int, username: str | None) -> bool:
    uname = username.lower().lstrip("@") if username else None
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        async with db.execute(
            "SELECT 1 FROM allowlist WHERE telegram_id = ? OR (username IS NOT NULL AND username = ?) LIMIT 1",
            (telegram_id, uname),
        ) as cur:
            return await cur.fetchone() is not None


async def find_user(telegram_id: int | None = None, username: str | None = None) -> dict | None:
    """Find a joined user by telegram_id (preferred) or username."""
    uname = username.lower().lstrip("@") if username else None
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        if telegram_id is not None:
            async with db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)) as cur:
                row = await cur.fetchone()
                if row:
                    return dict(row)
        if uname:
            async with db.execute("SELECT * FROM users WHERE username = ?", (uname,)) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None
    return None


async def delete_users_matching(telegram_id: int | None, username: str | None):
    uname = username.lower().lstrip("@") if username else None
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        if telegram_id is not None:
            await db.execute("DELETE FROM users WHERE telegram_id = ?", (telegram_id,))
        if uname:
            await db.execute("DELETE FROM users WHERE username = ?", (uname,))
        await db.commit()


# ---------------------------------------------------------------------------
# Vehicles
# ---------------------------------------------------------------------------

async def add_vehicle(name: str, plate_number: str) -> int:
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        cur = await db.execute(
            "INSERT INTO vehicles (name, plate_number) VALUES (?, ?)",
            (name, plate_number.upper()),
        )
        await db.commit()
        return cur.lastrowid


async def get_vehicle(vehicle_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM vehicles WHERE id = ?", (vehicle_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_all_vehicles() -> list[dict]:
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM vehicles ORDER BY name") as cur:
            return [dict(r) for r in await cur.fetchall()]


async def update_vehicle(vehicle_id: int, name: str | None = None, plate: str | None = None):
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        if name is not None:
            await db.execute("UPDATE vehicles SET name = ? WHERE id = ?", (name, vehicle_id))
        if plate is not None:
            await db.execute("UPDATE vehicles SET plate_number = ? WHERE id = ?", (plate.upper(), vehicle_id))
        await db.commit()


async def delete_vehicle(vehicle_id: int):
    """Delete a vehicle and everything attached (trackers, trips, checks, rmpds)."""
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        await db.execute(
            "DELETE FROM trip_checks WHERE trip_id IN (SELECT id FROM trips WHERE vehicle_id = ?)",
            (vehicle_id,),
        )
        await db.execute(
            "DELETE FROM trip_rmpds WHERE trip_id IN (SELECT id FROM trips WHERE vehicle_id = ?)",
            (vehicle_id,),
        )
        await db.execute("DELETE FROM trips WHERE vehicle_id = ?", (vehicle_id,))
        await db.execute("DELETE FROM trackers WHERE vehicle_id = ?", (vehicle_id,))
        await db.execute("DELETE FROM vehicles WHERE id = ?", (vehicle_id,))
        await db.commit()


# ---------------------------------------------------------------------------
# Trackers
# ---------------------------------------------------------------------------

async def add_tracker(vehicle_id: int, provider: str, tracker_number: str) -> int:
    """Add or replace a tracker for a (vehicle, role/provider) slot."""
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        cur = await db.execute(
            """INSERT INTO trackers (vehicle_id, provider, tracker_number)
               VALUES (?, ?, ?)
               ON CONFLICT(vehicle_id, provider)
               DO UPDATE SET tracker_number = excluded.tracker_number""",
            (vehicle_id, provider, tracker_number),
        )
        await db.commit()
        return cur.lastrowid


async def get_trackers_for_vehicle(vehicle_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM trackers WHERE vehicle_id = ? ORDER BY provider",
            (vehicle_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_tracker(tracker_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM trackers WHERE id = ?", (tracker_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def delete_tracker(tracker_id: int):
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        await db.execute("DELETE FROM trip_checks WHERE tracker_id = ?", (tracker_id,))
        await db.execute("DELETE FROM trackers WHERE id = ?", (tracker_id,))
        await db.commit()


# ---------------------------------------------------------------------------
# Trips
# ---------------------------------------------------------------------------

async def add_trip(vehicle_id: int, rmpd_number: str) -> int:
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        cur = await db.execute(
            "INSERT INTO trips (vehicle_id, rmpd_number) VALUES (?, ?)",
            (vehicle_id, rmpd_number),
        )
        trip_id = cur.lastrowid
        await db.execute(
            "INSERT INTO trip_rmpds (trip_id, rmpd_number) VALUES (?, ?)",
            (trip_id, rmpd_number),
        )
        await db.commit()
        return trip_id


async def add_trip_rmpd(trip_id: int, rmpd_number: str):
    """Stack a new RMPD onto a trip and make it the one we monitor (the latest)."""
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        await db.execute(
            "INSERT INTO trip_rmpds (trip_id, rmpd_number) VALUES (?, ?)",
            (trip_id, rmpd_number),
        )
        await db.execute(
            "UPDATE trips SET rmpd_number = ? WHERE id = ?", (rmpd_number, trip_id)
        )
        await db.commit()


async def get_trip_rmpds(trip_id: int) -> list[dict]:
    """RMPD history for a trip, oldest first (last = current)."""
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM trip_rmpds WHERE trip_id = ? ORDER BY id", (trip_id,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_trip(trip_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT t.*, v.name as vehicle_name, v.plate_number
               FROM trips t JOIN vehicles v ON v.id = t.vehicle_id
               WHERE t.id = ?""",
            (trip_id,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_active_trips() -> list[dict]:
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT t.*, v.name as vehicle_name, v.plate_number
               FROM trips t JOIN vehicles v ON v.id = t.vehicle_id
               WHERE t.status = 'active'
               ORDER BY t.created_at DESC"""
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def finish_trip(trip_id: int):
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        await db.execute(
            "UPDATE trips SET status='finished', finished_at=datetime('now') WHERE id=?",
            (trip_id,),
        )
        await db.commit()


async def delete_trip(trip_id: int):
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        await db.execute("DELETE FROM trip_checks WHERE trip_id = ?", (trip_id,))
        await db.execute("DELETE FROM trip_rmpds WHERE trip_id = ?", (trip_id,))
        await db.execute("DELETE FROM trips WHERE id = ?", (trip_id,))
        await db.commit()


async def set_trip_alarm_stage(trip_id: int, stage: int):
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        await db.execute("UPDATE trips SET alarm_stage = ? WHERE id = ?", (stage, trip_id))
        await db.commit()


# ---------------------------------------------------------------------------
# Trip checks
# ---------------------------------------------------------------------------

async def save_check(
    trip_id: int,
    tracker_id: int,
    status: str,
    last_position_time,
    raw_message: str,
    latitude: float | None = None,
    longitude: float | None = None,
    alarm: bool = False,
) -> int:
    lpt = last_position_time.isoformat() if last_position_time else None
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        cur = await db.execute(
            """INSERT INTO trip_checks
               (trip_id, tracker_id, status, last_position_time, latitude, longitude, alarm, raw_message)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (trip_id, tracker_id, status, lpt, latitude, longitude, int(alarm), raw_message),
        )
        await db.commit()
        return cur.lastrowid


async def get_last_check(trip_id: int, tracker_id: int) -> dict | None:
    """Return the most recent check for a trip+tracker pair."""
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM trip_checks
               WHERE trip_id = ? AND tracker_id = ?
               ORDER BY id DESC LIMIT 1""",
            (trip_id, tracker_id),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def prune_old_checks(days: int = 14) -> int:
    """Delete trip_checks rows older than `days` days. Returns rows deleted.

    Recent history (last ~20 checks ≈ 100 min) is always far newer than the
    retention window, so movement detection / effective_status are unaffected.
    """
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        cur = await db.execute(
            "DELETE FROM trip_checks WHERE created_at < datetime('now', ?)",
            (f"-{int(days)} days",),
        )
        await db.commit()
        return cur.rowcount


# ---------------------------------------------------------------------------
# News
# ---------------------------------------------------------------------------

async def is_news_seen(article_id: str) -> bool:
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        async with db.execute("SELECT 1 FROM news_seen WHERE article_id = ?", (article_id,)) as cur:
            return await cur.fetchone() is not None


async def mark_news_seen(article_id: str, title: str):
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        await db.execute(
            "INSERT OR IGNORE INTO news_seen (article_id, title) VALUES (?, ?)",
            (article_id, title),
        )
        await db.commit()


async def count_news_seen() -> int:
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        async with db.execute("SELECT COUNT(*) FROM news_seen") as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def get_check_history(trip_id: int, tracker_id: int, limit: int = 20) -> list[dict]:
    """Return recent checks (newest first) — used for movement detection."""
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM trip_checks
               WHERE trip_id = ? AND tracker_id = ?
               ORDER BY id DESC LIMIT ?""",
            (trip_id, tracker_id, limit),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_recent_checks_for_trip(trip_id: int, limit: int = 10) -> list[dict]:
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT tc.*, t.provider, t.tracker_number
               FROM trip_checks tc
               JOIN trackers t ON t.id = tc.tracker_id
               WHERE tc.trip_id = ?
               ORDER BY tc.created_at DESC LIMIT ?""",
            (trip_id, limit),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]
