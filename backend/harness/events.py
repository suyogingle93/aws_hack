"""Event model and an in-process pub/sub bus.

The bus never blocks the proxy: every subscriber has a bounded queue, and when
a slow consumer falls behind its oldest events are dropped (and counted).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import AsyncIterator, Literal

from pydantic import BaseModel, Field

TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"  # fixed width so string order == time order

EventKind = Literal["request", "run_started", "run_stopped", "run_finished", "policy_reloaded"]
Decision = Literal["allowed", "blocked"]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def format_ts(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime(TS_FORMAT)


class Event(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    run_id: str
    ts: datetime = Field(default_factory=utcnow)
    kind: EventKind = "request"
    method: str | None = None
    host: str | None = None
    path: str | None = None
    tool: str | None = None
    decision: Decision | None = None
    rule_id: str | None = None
    reason: str | None = None
    status_code: int | None = None
    headers: dict[str, str] = Field(default_factory=dict)  # must already be redacted


class Subscription:
    """A bounded queue of events for one consumer."""

    def __init__(self, bus: "EventBus", run_id: str | None, maxsize: int) -> None:
        self._bus = bus
        self.run_id = run_id
        self._queue: asyncio.Queue[Event | None] = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0
        self._closed = False

    def _put(self, item: Event | None) -> None:
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            self._queue.get_nowait()  # drop oldest
            self.dropped += 1
            self._queue.put_nowait(item)

    def deliver(self, event: Event) -> None:
        if self._closed:
            return
        if self.run_id is None or event.run_id == self.run_id:
            self._put(event)

    async def get(self) -> Event | None:
        """Next event, or None once the subscription is closed and drained."""
        return await self._queue.get()

    def close(self) -> None:
        """Stop receiving. Events already queued are still delivered first."""
        if self._closed:
            return
        self._closed = True
        self._bus._remove(self)
        self._put(None)

    async def __aiter__(self) -> AsyncIterator[Event]:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item


class EventBus:
    def __init__(self) -> None:
        self._subs: list[Subscription] = []

    def subscribe(self, run_id: str | None = None, maxsize: int = 1000) -> Subscription:
        sub = Subscription(self, run_id, maxsize)
        self._subs.append(sub)
        return sub

    def publish(self, event: Event) -> None:
        for sub in list(self._subs):
            sub.deliver(event)

    def _remove(self, sub: Subscription) -> None:
        if sub in self._subs:
            self._subs.remove(sub)

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)
