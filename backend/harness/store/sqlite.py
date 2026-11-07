from __future__ import annotations

import sqlite3
import threading
from datetime import datetime

from ..events import Event, format_ts
from .base import AuditStore, RunRecord

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id      TEXT PRIMARY KEY,
    run_id  TEXT NOT NULL,
    ts      TEXT NOT NULL,
    data    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_run_ts ON events (run_id, ts);

CREATE TABLE IF NOT EXISTS runs (
    id          TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    data        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs (started_at);
"""


class SqliteStore(AuditStore):
    def __init__(self, path: str = "harness.sqlite3") -> None:
        # One connection shared across threads, guarded by a lock.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def put_event(self, event: Event) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO events (id, run_id, ts, data) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET data = excluded.data",
                (event.id, event.run_id, format_ts(event.ts), event.model_dump_json()),
            )
            self._conn.commit()

    def list_events(
        self,
        run_id: str,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 1000,
    ) -> list[Event]:
        sql = "SELECT data FROM events WHERE run_id = ?"
        args: list[object] = [run_id]
        if since is not None:
            sql += " AND ts >= ?"
            args.append(format_ts(since))
        if until is not None:
            sql += " AND ts <= ?"
            args.append(format_ts(until))
        sql += " ORDER BY ts ASC, rowid ASC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [Event.model_validate_json(r[0]) for r in rows]

    def put_run(self, run: RunRecord) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO runs (id, started_at, data) VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET data = excluded.data",
                (run.id, format_ts(run.started_at), run.model_dump_json()),
            )
            self._conn.commit()

    def get_run(self, run_id: str) -> RunRecord | None:
        with self._lock:
            row = self._conn.execute("SELECT data FROM runs WHERE id = ?", (run_id,)).fetchone()
        return RunRecord.model_validate_json(row[0]) if row else None

    def list_runs(self, limit: int = 100) -> list[RunRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [RunRecord.model_validate_json(r[0]) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
