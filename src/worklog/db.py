"""SQLite state — clients + sessions.

The partial unique index on `status='active'` is the enforcement mechanism
for "at most one active session"; the second `start` without a stop fails at
the DB layer, not in Python.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from contextlib import contextmanager
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT UNIQUE NOT NULL,
    hours_budget_weekly REAL,
    rate                REAL,
    active              INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS sessions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id    INTEGER REFERENCES clients(id),
    started_at   TEXT NOT NULL,
    stopped_at   TEXT,
    note         TEXT,
    tags         TEXT,
    summary_path TEXT,
    duration_s   INTEGER,
    status       TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('active','closed','abandoned'))
);

CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active
    ON sessions(status) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS session_notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    added_at   TEXT    NOT NULL,
    text       TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notes_session ON session_notes(session_id);
"""


@contextmanager
def connect(db_path: Path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
    try:
        yield con
    finally:
        con.close()


def iso_utc(now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    return now.astimezone(dt.timezone.utc).isoformat()


def active_session(con: sqlite3.Connection) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM sessions WHERE status='active'").fetchone()


def start_session(
    con: sqlite3.Connection,
    started_at: str,
    client_id: int | None,
    note: str | None,
    tags: list[str] | None,
) -> int:
    cur = con.execute(
        "INSERT INTO sessions(client_id, started_at, note, tags, status) "
        "VALUES (?, ?, ?, ?, 'active')",
        (client_id, started_at, note, ",".join(tags) if tags else None),
    )
    return cur.lastrowid


def close_session(
    con: sqlite3.Connection,
    session_id: int,
    stopped_at: str,
    duration_s: int,
    summary_path: str | None,
) -> None:
    con.execute(
        "UPDATE sessions "
        "SET stopped_at=?, duration_s=?, summary_path=?, status='closed' "
        "WHERE id=?",
        (stopped_at, duration_s, summary_path, session_id),
    )


def abandon_active(con: sqlite3.Connection) -> int:
    cur = con.execute(
        "UPDATE sessions SET status='abandoned', stopped_at=? "
        "WHERE status='active'",
        (iso_utc(),),
    )
    return cur.rowcount


def get_client_by_name(con: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM clients WHERE name=?", (name,)).fetchone()


def upsert_client(
    con: sqlite3.Connection,
    name: str,
    hours_budget_weekly: float | None = None,
    rate: float | None = None,
    active: bool = True,
) -> int:
    existing = get_client_by_name(con, name)
    if existing is None:
        cur = con.execute(
            "INSERT INTO clients(name, hours_budget_weekly, rate, active) "
            "VALUES (?, ?, ?, ?)",
            (name, hours_budget_weekly, rate, 1 if active else 0),
        )
        return cur.lastrowid
    con.execute(
        "UPDATE clients SET "
        "hours_budget_weekly=COALESCE(?, hours_budget_weekly), "
        "rate=COALESCE(?, rate), "
        "active=? "
        "WHERE id=?",
        (hours_budget_weekly, rate, 1 if active else 0, existing["id"]),
    )
    return existing["id"]


def list_clients(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(con.execute("SELECT * FROM clients ORDER BY active DESC, name"))


def sessions_in_range(
    con: sqlite3.Connection,
    start_iso: str,
    end_iso: str,
    client_id: int | None = None,
) -> list[sqlite3.Row]:
    q = (
        "SELECT s.*, c.name AS client_name, c.hours_budget_weekly "
        "FROM sessions s LEFT JOIN clients c ON c.id = s.client_id "
        "WHERE s.started_at >= ? AND s.started_at < ? AND s.status='closed'"
    )
    params: list = [start_iso, end_iso]
    if client_id is not None:
        q += " AND s.client_id = ?"
        params.append(client_id)
    q += " ORDER BY s.started_at"
    return list(con.execute(q, params))


def get_session(con: sqlite3.Connection, session_id: int) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()


def recent_sessions(con: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return list(
        con.execute(
            "SELECT s.*, c.name AS client_name "
            "FROM sessions s LEFT JOIN clients c ON c.id = s.client_id "
            "ORDER BY s.started_at DESC LIMIT ?",
            (limit,),
        )
    )


def update_session(
    con: sqlite3.Connection,
    session_id: int,
    started_at: str | None = None,
    stopped_at: str | None = None,
    client_id: int | None = None,
    note: str | None = None,
) -> None:
    fields, params = [], []
    if started_at is not None:
        fields.append("started_at=?")
        params.append(started_at)
    if stopped_at is not None:
        fields.append("stopped_at=?")
        params.append(stopped_at)
    if client_id is not None:
        fields.append("client_id=?")
        params.append(client_id)
    if note is not None:
        fields.append("note=?")
        params.append(note)
    if not fields:
        return
    params.append(session_id)
    con.execute(f"UPDATE sessions SET {', '.join(fields)} WHERE id=?", params)
    if started_at is not None or stopped_at is not None:
        row = get_session(con, session_id)
        if row and row["started_at"] and row["stopped_at"]:
            s = dt.datetime.fromisoformat(row["started_at"])
            e = dt.datetime.fromisoformat(row["stopped_at"])
            con.execute(
                "UPDATE sessions SET duration_s=? WHERE id=?",
                (int((e - s).total_seconds()), session_id),
            )


def update_session_tags(
    con: sqlite3.Connection, session_id: int, tags: list[str]
) -> None:
    con.execute(
        "UPDATE sessions SET tags=? WHERE id=?",
        (",".join(tags) if tags else None, session_id),
    )


# ---------------------------------------------------------------------------
# notes
# ---------------------------------------------------------------------------

def add_note(
    con: sqlite3.Connection,
    session_id: int,
    text: str,
    added_at: str | None = None,
) -> int:
    cur = con.execute(
        "INSERT INTO session_notes(session_id, added_at, text) VALUES (?, ?, ?)",
        (session_id, added_at or iso_utc(), text),
    )
    return cur.lastrowid


def list_notes(
    con: sqlite3.Connection, session_id: int
) -> list[sqlite3.Row]:
    return list(
        con.execute(
            "SELECT * FROM session_notes WHERE session_id=? ORDER BY added_at, id",
            (session_id,),
        )
    )


def get_note(con: sqlite3.Connection, note_id: int) -> sqlite3.Row | None:
    return con.execute(
        "SELECT * FROM session_notes WHERE id=?", (note_id,)
    ).fetchone()


def delete_note(con: sqlite3.Connection, note_id: int) -> int:
    cur = con.execute("DELETE FROM session_notes WHERE id=?", (note_id,))
    return cur.rowcount
