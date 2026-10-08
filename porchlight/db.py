"""SQLite store. One file, no ORM; rows come back as plain dicts."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Raw Ring events, exactly as received (webhook) or listed (event history).
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    type TEXT NOT NULL,
    sub_type TEXT,
    ts INTEGER NOT NULL,
    end_ts INTEGER,
    source TEXT NOT NULL,
    raw TEXT NOT NULL,
    applied INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);

CREATE TABLE IF NOT EXISTS webhook_requests (
    request_id TEXT PRIMARY KEY,
    ts INTEGER NOT NULL
);

-- What a frame showed. An observation only moves the care plan once it is trusted.
CREATE TABLE IF NOT EXISTS observations (
    id TEXT PRIMARY KEY,
    event_id TEXT,
    device_id TEXT,
    ts INTEGER NOT NULL,
    frame_source TEXT NOT NULL,
    snapshot_file TEXT,
    snapshot_sha256 TEXT,
    provider TEXT NOT NULL,
    model TEXT,
    package_present INTEGER,
    person_present INTEGER,
    vehicle_present INTEGER,
    confidence REAL NOT NULL DEFAULT 0,
    summary TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    reviewed_by TEXT,
    reviewed_at INTEGER,
    applied INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS observations_ts ON observations (ts);

CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    kind TEXT NOT NULL,
    start_minute INTEGER NOT NULL,
    duration_min INTEGER NOT NULL,
    collect_within_min INTEGER,
    repeat TEXT NOT NULL,
    weekday INTEGER,
    active INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS expectations (
    id TEXT PRIMARY KEY,
    plan_id TEXT,
    title TEXT NOT NULL,
    kind TEXT NOT NULL,
    window_start INTEGER NOT NULL,
    window_end INTEGER NOT NULL,
    collect_within_min INTEGER,
    state TEXT NOT NULL,
    unplanned INTEGER NOT NULL DEFAULT 0,
    arrived_at INTEGER,
    completed_at INTEGER,
    arrival_observation TEXT,
    completion_observation TEXT,
    completion_event TEXT,
    note TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS expectations_window ON expectations (window_start);
CREATE UNIQUE INDEX IF NOT EXISTS expectations_plan_day ON expectations (plan_id, window_start)
    WHERE plan_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS alerts (
    id TEXT PRIMARY KEY,
    ts INTEGER NOT NULL,
    level TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    expectation_id TEXT,
    evidence TEXT NOT NULL DEFAULT '[]',
    state TEXT NOT NULL DEFAULT 'open',
    ack_by TEXT,
    ack_at INTEGER,
    dedupe_key TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS alerts_ts ON alerts (ts);

-- Things Porchlight or the assistant would like to do. Nothing runs until a person approves.
CREATE TABLE IF NOT EXISTS actions (
    id TEXT PRIMARY KEY,
    ts INTEGER NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    proposed_by TEXT NOT NULL,
    expectation_id TEXT,
    alert_id TEXT,
    status TEXT NOT NULL DEFAULT 'proposed',
    decided_by TEXT,
    decided_at INTEGER,
    result TEXT,
    dedupe_key TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS contacts (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT ''
);

-- Append-only ledger: every state change, the rule that made it, and its evidence.
CREATE TABLE IF NOT EXISTS ledger (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    actor TEXT NOT NULL,
    rule_id TEXT,
    subject TEXT NOT NULL,
    what TEXT NOT NULL,
    evidence TEXT NOT NULL DEFAULT '[]',
    detail TEXT NOT NULL DEFAULT '{}'
);
"""

_BOOL_COLUMNS = {
    "package_present",
    "person_present",
    "vehicle_present",
    "applied",
    "unplanned",
    "active",
}
_JSON_COLUMNS = {"raw", "evidence", "detail"}


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _decode(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out: dict[str, Any] = {}
    for key in row.keys():
        value = row[key]
        if key in _BOOL_COLUMNS and value is not None:
            value = bool(value)
        elif key in _JSON_COLUMNS and isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                pass
        out[key] = value
    return out


def _encode(values: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for key, value in values.items():
        if isinstance(value, bool):
            value = int(value)
        elif isinstance(value, (dict, list)):
            value = json.dumps(value)
        out[key] = value
    return out


class Store:
    def __init__(self, path: str | Path = ":memory:") -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- generic helpers -------------------------------------------------

    def insert(self, table: str, values: dict[str, Any], *, ignore: bool = False) -> bool:
        values = _encode(values)
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        verb = "INSERT OR IGNORE" if ignore else "INSERT"
        with self._lock:
            cur = self._conn.execute(f"{verb} INTO {table} ({cols}) VALUES ({marks})", list(values.values()))
            return cur.rowcount > 0

    def update(self, table: str, row_id: str, values: dict[str, Any]) -> None:
        values = _encode(values)
        sets = ", ".join(f"{k} = ?" for k in values)
        with self._lock:
            self._conn.execute(f"UPDATE {table} SET {sets} WHERE id = ?", [*values.values(), row_id])

    def get(self, table: str, row_id: str) -> dict[str, Any] | None:
        with self._lock:
            return _decode(self._conn.execute(f"SELECT * FROM {table} WHERE id = ?", [row_id]).fetchone())

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(sql, list(params)).fetchall()
        return [d for d in (_decode(r) for r in rows) if d is not None]

    def one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self._lock:
            return self._conn.execute(sql, list(params)).rowcount

    # -- meta ------------------------------------------------------------

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM meta WHERE key = ?", [key])
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [key, value],
        )

    # -- ledger ----------------------------------------------------------

    def record(
        self,
        ts: int,
        actor: str,
        subject: str,
        what: str,
        *,
        rule_id: str | None = None,
        evidence: list[str] | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.insert(
            "ledger",
            {
                "ts": ts,
                "actor": actor,
                "rule_id": rule_id,
                "subject": subject,
                "what": what,
                "evidence": evidence or [],
                "detail": detail or {},
            },
        )
