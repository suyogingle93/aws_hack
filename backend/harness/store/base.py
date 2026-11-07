from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from ..events import Event


class RunRecord(BaseModel):
    """Persisted metadata for one run (never contains the proxy token)."""

    id: str
    command: list[str]
    cwd: str
    status: Literal["running", "stopped", "finished"] = "running"
    pid: int | None = None
    started_at: datetime
    ended_at: datetime | None = None
    exit_code: int | None = None


class AuditStore(ABC):
    """Durable record of runs and events. Writes are upserts keyed by id."""

    @abstractmethod
    def put_event(self, event: Event) -> None: ...

    @abstractmethod
    def list_events(
        self,
        run_id: str,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 1000,
    ) -> list[Event]:
        """Events for a run in ascending time order."""

    @abstractmethod
    def put_run(self, run: RunRecord) -> None: ...

    @abstractmethod
    def get_run(self, run_id: str) -> RunRecord | None: ...

    @abstractmethod
    def list_runs(self, limit: int = 100) -> list[RunRecord]:
        """Most recently started first."""

    def close(self) -> None:  # optional
        return None
