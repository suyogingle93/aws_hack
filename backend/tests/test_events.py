import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from harness.events import Event, EventBus, format_ts
from harness.store import RunRecord, SqliteStore, StoreWriter
from harness.store.base import AuditStore


def ev(run_id="r1", **kw):
    return Event(run_id=run_id, **kw)


# --- bus ------------------------------------------------------------------

async def test_subscriber_receives_events_in_order():
    bus = EventBus()
    sub = bus.subscribe()
    a, b = ev(host="a"), ev(host="b")
    bus.publish(a)
    bus.publish(b)
    assert (await sub.get()).host == "a"
    assert (await sub.get()).host == "b"


async def test_run_filter():
    bus = EventBus()
    sub = bus.subscribe(run_id="r1")
    bus.publish(ev("r2", host="other"))
    bus.publish(ev("r1", host="mine"))
    assert (await sub.get()).host == "mine"


async def test_slow_subscriber_drops_oldest_without_blocking_publisher():
    bus = EventBus()
    sub = bus.subscribe(maxsize=2)
    for h in ("a", "b", "c", "d"):
        bus.publish(ev(host=h))  # must not raise or block
    assert sub.dropped == 2
    assert [(await sub.get()).host, (await sub.get()).host] == ["c", "d"]


async def test_close_delivers_queued_then_ends_and_unsubscribes():
    bus = EventBus()
    sub = bus.subscribe()
    bus.publish(ev(host="a"))
    sub.close()
    bus.publish(ev(host="late"))
    assert bus.subscriber_count == 0
    assert [e.host async for e in sub] == ["a"]


async def test_close_on_full_queue_still_terminates():
    bus = EventBus()
    sub = bus.subscribe(maxsize=1)
    bus.publish(ev(host="a"))
    sub.close()
    assert await asyncio.wait_for(sub.get(), 1) is None


def test_event_ids_are_unique():
    assert ev().id != ev().id


# --- sqlite store ---------------------------------------------------------

@pytest.fixture
def store():
    s = SqliteStore(":memory:")
    yield s
    s.close()


T0 = datetime(2026, 11, 7, 12, 0, 0, tzinfo=timezone.utc)


def test_event_roundtrip_and_upsert(store: AuditStore):
    e = ev(host="x.com", decision="allowed", headers={"a": "b"}, ts=T0)
    store.put_event(e)
    store.put_event(e.model_copy(update={"status_code": 200}))  # response update, same id
    got = store.list_events("r1")
    assert len(got) == 1 and got[0].status_code == 200 and got[0].headers == {"a": "b"}


def test_query_by_run_and_time_range(store: AuditStore):
    for i in range(5):
        store.put_event(ev("r1", host=f"h{i}", ts=T0 + timedelta(seconds=i)))
    store.put_event(ev("r2", host="other", ts=T0))
    assert [e.host for e in store.list_events("r1")] == ["h0", "h1", "h2", "h3", "h4"]
    mid = store.list_events("r1", since=T0 + timedelta(seconds=1), until=T0 + timedelta(seconds=3))
    assert [e.host for e in mid] == ["h1", "h2", "h3"]
    assert len(store.list_events("r1", limit=2)) == 2


def test_ordering_is_correct_across_whole_seconds(store: AuditStore):
    # Timestamps with 0 microseconds must still sort correctly against fractional ones.
    store.put_event(ev(host="late", ts=T0 + timedelta(seconds=1)))
    store.put_event(ev(host="early", ts=T0 + timedelta(milliseconds=500)))
    assert [e.host for e in store.list_events("r1")] == ["early", "late"]


def test_format_ts_is_fixed_width():
    assert len(format_ts(T0)) == len(format_ts(T0 + timedelta(microseconds=5)))


def test_runs_roundtrip_and_ordering(store: AuditStore):
    older = RunRecord(id="a", command=["x"], cwd="/", started_at=T0)
    newer = RunRecord(id="b", command=["y"], cwd="/", started_at=T0 + timedelta(minutes=1))
    store.put_run(older)
    store.put_run(newer)
    store.put_run(older.model_copy(update={"status": "stopped", "exit_code": -15}))
    assert [r.id for r in store.list_runs()] == ["b", "a"]
    got = store.get_run("a")
    assert got.status == "stopped" and got.exit_code == -15
    assert store.get_run("missing") is None


# --- writer ---------------------------------------------------------------

async def test_writer_persists_published_events():
    bus, store = EventBus(), SqliteStore(":memory:")
    writer = StoreWriter(bus, store)
    writer.start()
    for i in range(3):
        bus.publish(ev(host=f"h{i}"))
    await writer.stop()  # flushes
    assert [e.host for e in store.list_events("r1")] == ["h0", "h1", "h2"]


class FlakyStore(SqliteStore):
    def __init__(self, fail_times):
        super().__init__(":memory:")
        self.fail_times = fail_times

    def put_event(self, event):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("boom")
        super().put_event(event)


async def test_writer_retries_then_succeeds():
    bus, store = EventBus(), FlakyStore(fail_times=2)
    writer = StoreWriter(bus, store, retries=3, backoff=0.01)
    writer.start()
    bus.publish(ev(host="x"))
    await writer.stop()
    assert len(store.list_events("r1")) == 1 and writer.failed == 0


async def test_writer_gives_up_but_keeps_going():
    bus, store = EventBus(), FlakyStore(fail_times=3)
    writer = StoreWriter(bus, store, retries=3, backoff=0.01)
    writer.start()
    bus.publish(ev(host="lost"))
    bus.publish(ev(host="kept"))
    await writer.stop()
    assert writer.failed == 1
    assert [e.host for e in store.list_events("r1")] == ["kept"]
