"""Run registry, per-run proxy credentials, and the kill switch."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import secrets
import signal
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from .events import Event, EventBus, utcnow
from .store.base import AuditStore, RunRecord

log = logging.getLogger("harness.runs")

POLL_INTERVAL = 0.05


class RunNotFound(KeyError):
    pass


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class _RunState:
    record: RunRecord
    token_hash: str | None  # None once revoked
    allowed: int = 0
    blocked: int = 0


@dataclass(frozen=True)
class RunView:
    record: RunRecord
    allowed: int
    blocked: int


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


async def terminate_process_group(pgid: int, grace: float = 2.0) -> None:
    """SIGTERM the group, wait up to `grace` seconds, then SIGKILL what remains.

    Refuses to touch pgid <= 1 or the caller's own group, so a bad pid can never
    take down the backend or the system.
    """
    if pgid <= 1 or pgid == os.getpgrp():
        raise ValueError(f"refusing to signal process group {pgid}")
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = asyncio.get_running_loop().time() + grace
    while _group_alive(pgid):
        if asyncio.get_running_loop().time() >= deadline:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                return
            while _group_alive(pgid) and asyncio.get_running_loop().time() < deadline + 1.0:
                await asyncio.sleep(POLL_INTERVAL)
            return
        await asyncio.sleep(POLL_INTERVAL)


class RunManager:
    def __init__(
        self,
        bus: EventBus,
        store: AuditStore | None = None,
        grace: float = 2.0,
        on_run_end: Callable[[str], None] | None = None,
    ) -> None:
        self._bus = bus
        self._store = store
        self._grace = grace
        self._on_run_end = on_run_end
        self._runs: dict[str, _RunState] = {}
        self._stop_locks: dict[str, asyncio.Lock] = {}

    # --- lifecycle ---------------------------------------------------------

    def register(self, command: list[str], cwd: str) -> tuple[RunRecord, str]:
        """Create a run. The returned token is shown once and only its hash is kept."""
        run_id = secrets.token_hex(4)
        while run_id in self._runs:
            run_id = secrets.token_hex(4)
        token = secrets.token_urlsafe(32)
        record = RunRecord(id=run_id, command=command, cwd=cwd, started_at=utcnow())
        self._runs[run_id] = _RunState(record=record, token_hash=_hash_token(token))
        self._persist(record)
        self._bus.publish(Event(run_id=run_id, kind="run_started", reason=" ".join(command)))
        return record, token

    def set_pid(self, run_id: str, pid: int) -> None:
        """Record the child's pid. The launcher starts it as a session leader (pgid == pid)."""
        state = self._get(run_id)
        state.record = state.record.model_copy(update={"pid": pid})
        self._persist(state.record)

    def finish(self, run_id: str, exit_code: int) -> None:
        """The launcher reports that the process exited."""
        state = self._get(run_id)
        if state.record.ended_at is not None:
            # Already stopped: keep status "stopped" but record how the process ended.
            state.record = state.record.model_copy(update={"exit_code": exit_code})
            self._persist(state.record)
            return
        state.token_hash = None
        state.record = state.record.model_copy(
            update={"status": "finished", "ended_at": utcnow(), "exit_code": exit_code}
        )
        self._persist(state.record)
        self._bus.publish(Event(run_id=run_id, kind="run_finished", reason=f"exit code {exit_code}"))
        self._ended(run_id)

    async def stop(self, run_id: str, actor: str = "operator", reason: str = "stopped by operator") -> bool:
        """Kill switch. Returns True if this call stopped the run, False if it was already over.

        The token is revoked first so the gateway rejects further requests even
        while the process is still shutting down.
        """
        state = self._get(run_id)
        lock = self._stop_locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            if state.record.status != "running":
                return False
            state.token_hash = None
            state.record = state.record.model_copy(update={"status": "stopped", "ended_at": utcnow()})
            self._persist(state.record)
            self._bus.publish(Event(run_id=run_id, kind="run_stopped", reason=f"{actor}: {reason}"))
            self._ended(run_id)
            await self._kill(state.record)
            return True

    async def _kill(self, record: RunRecord) -> None:
        pid = record.pid
        if pid is None:
            return
        try:
            pgid = os.getpgid(pid)
        except ProcessLookupError:
            return  # already gone
        if pgid != pid:
            # Not a session leader started by the launcher; only signal that one process.
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            return
        try:
            await terminate_process_group(pgid, self._grace)
        except ValueError:
            log.error("refused to signal process group %s for run %s", pgid, record.id)

    # --- auth --------------------------------------------------------------

    def authenticate(self, run_id: str, token: str) -> bool:
        state = self._runs.get(run_id)
        # Compare against a dummy hash for unknown/revoked runs to keep timing uniform.
        expected = state.token_hash if state and state.token_hash else _hash_token("")
        ok = hmac.compare_digest(expected, _hash_token(token))
        return bool(state and state.token_hash and ok)

    def is_running(self, run_id: str) -> bool:
        """True while the run is active. Used for requests inside an already-authenticated tunnel."""
        state = self._runs.get(run_id)
        return bool(state and state.record.status == "running")

    # --- accounting & queries ----------------------------------------------

    def record_decision(self, run_id: str, allowed: bool) -> None:
        state = self._runs.get(run_id)
        if state is None:
            return
        if allowed:
            state.allowed += 1
        else:
            state.blocked += 1

    def get(self, run_id: str) -> RunView:
        state = self._get(run_id)
        return RunView(state.record, state.allowed, state.blocked)

    def list_runs(self) -> list[RunView]:
        views = [RunView(s.record, s.allowed, s.blocked) for s in self._runs.values()]
        return sorted(views, key=lambda v: v.record.started_at, reverse=True)

    # --- internals ---------------------------------------------------------

    def _get(self, run_id: str) -> _RunState:
        try:
            return self._runs[run_id]
        except KeyError:
            raise RunNotFound(run_id) from None

    def _ended(self, run_id: str) -> None:
        if self._on_run_end:
            try:
                self._on_run_end(run_id)
            except Exception:  # noqa: BLE001
                log.exception("on_run_end callback failed")

    def _persist(self, record: RunRecord) -> None:
        if self._store is None:
            return
        try:
            self._store.put_run(record)
        except Exception:  # noqa: BLE001 - never let storage break the kill switch
            log.exception("failed to persist run %s", record.id)
