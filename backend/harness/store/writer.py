"""Background task that persists bus events without ever blocking the proxy."""

from __future__ import annotations

import asyncio
import logging

from ..events import Event, EventBus
from .base import AuditStore

log = logging.getLogger("harness.store")


class StoreWriter:
    def __init__(self, bus: EventBus, store: AuditStore, retries: int = 3, backoff: float = 0.2) -> None:
        self._bus = bus
        self._store = store
        self._retries = retries
        self._backoff = backoff
        self._sub = None
        self._task: asyncio.Task | None = None
        self.failed = 0  # events given up on after all retries

    def start(self) -> None:
        if self._task is not None:
            return
        self._sub = self._bus.subscribe(maxsize=10_000)
        self._task = asyncio.create_task(self._run(), name="store-writer")

    async def stop(self) -> None:
        """Stop accepting events and flush everything already queued."""
        if self._sub is None or self._task is None:
            return
        self._sub.close()
        await self._task
        self._task = None
        self._sub = None

    async def _run(self) -> None:
        assert self._sub is not None
        async for event in self._sub:
            await self._write(event)

    async def _write(self, event: Event) -> None:
        for attempt in range(self._retries):
            try:
                await asyncio.to_thread(self._store.put_event, event)
                return
            except Exception:  # noqa: BLE001 - the store may raise anything
                log.warning("store write failed (attempt %d/%d) for event %s", attempt + 1, self._retries, event.id)
                if attempt + 1 < self._retries:
                    await asyncio.sleep(self._backoff * (2**attempt))
        self.failed += 1
        log.error("giving up on event %s after %d attempts", event.id, self._retries)
