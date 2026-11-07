"""Fast local primary + durable remote mirror.

Reads and writes go to the primary (SQLite) synchronously, so the dashboard, the CLI, and
the kill switch never wait on the network. Every write is then replayed, in order, to the
mirror (DynamoDB) on one background thread with retries. If AWS is slow or unreachable the
harness keeps enforcing policy; the mirror catches up when it can.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import datetime

from ..events import Event
from .base import AuditStore, RunRecord

log = logging.getLogger("harness.store.mirror")

_STOP = object()


class MirroredStore(AuditStore):
    def __init__(
        self,
        primary: AuditStore,
        mirror: AuditStore,
        retries: int = 3,
        backoff: float = 0.5,
        maxsize: int = 50_000,
    ) -> None:
        self.primary = primary
        self.mirror = mirror
        self._retries = retries
        self._backoff = backoff
        self._queue: queue.Queue = queue.Queue(maxsize=maxsize)
        self.mirrored = 0  # writes delivered to the mirror
        self.failed = 0  # writes given up on after all retries
        self.dropped = 0  # writes never queued because the mirror fell too far behind
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="audit-mirror", daemon=True)
        self._thread.start()

    # --- writes: primary now, mirror in the background ------------------------------

    def put_event(self, event: Event) -> None:
        self.primary.put_event(event)
        self._enqueue(self.mirror.put_event, event)

    def put_run(self, run: RunRecord) -> None:
        self.primary.put_run(run)
        self._enqueue(self.mirror.put_run, run)

    def _enqueue(self, write, item) -> None:
        if self._closed:
            return
        try:
            self._queue.put_nowait((write, item))
        except queue.Full:
            self.dropped += 1
            log.warning("audit mirror is %d writes behind; dropping a write", self._queue.qsize())

    def _run(self) -> None:
        while True:
            entry = self._queue.get()
            try:
                if entry is _STOP:
                    return
                write, item = entry
                for attempt in range(self._retries):
                    try:
                        write(item)
                        self.mirrored += 1
                        break
                    except Exception as e:  # noqa: BLE001 - boto3 can raise many things
                        log.warning("audit mirror write failed (attempt %d/%d): %s", attempt + 1, self._retries, e)
                        if attempt + 1 < self._retries:
                            time.sleep(self._backoff * (2**attempt))
                else:
                    self.failed += 1
            finally:
                self._queue.task_done()

    def flush(self, timeout: float = 10.0) -> bool:
        """Wait until everything queued so far has been mirrored (or given up on)."""
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)
        return True

    # --- reads: always local ------------------------------------------------------------

    def list_events(
        self,
        run_id: str,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 1000,
    ) -> list[Event]:
        return self.primary.list_events(run_id, since, until, limit)

    def get_run(self, run_id: str) -> RunRecord | None:
        return self.primary.get_run(run_id)

    def list_runs(self, limit: int = 100) -> list[RunRecord]:
        return self.primary.list_runs(limit)

    def close(self, timeout: float = 15.0) -> None:
        if self._closed:
            return
        self._closed = True
        self._queue.put(_STOP)  # after everything already queued, so pending writes flush
        self._thread.join(timeout)
        if self._thread.is_alive():
            log.error("audit mirror did not finish flushing within %.0fs", timeout)
        self.primary.close()
        self.mirror.close()
