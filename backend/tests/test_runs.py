import os
import signal
import subprocess
import sys
import threading
import time

import pytest

from harness.events import EventBus
from harness.runs import RunManager, RunNotFound, terminate_process_group
from harness.store import SqliteStore


def spawn(code: str) -> subprocess.Popen:
    """Start a child as its own session/process group, like the launcher does."""
    proc = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
    # The launcher reaps its child; do the same so we never see a zombie.
    threading.Thread(target=proc.wait, daemon=True).start()
    return proc


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def wait_dead(pids, timeout=3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not any(alive(p) for p in pids):
            return True
        time.sleep(0.02)
    return False


# Parent spawns a grandchild, prints its pid, then both sleep.
TREE = """
import subprocess, sys, time
g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
print(g.pid, flush=True)
time.sleep(60)
"""

STUBBORN = """
import signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
print("ready", flush=True)
while True:
    time.sleep(0.1)
"""


@pytest.fixture
def mgr():
    bus = EventBus()
    return RunManager(bus, SqliteStore(":memory:"), grace=0.5), bus


# --- credentials ------------------------------------------------------------

def test_register_and_authenticate(mgr):
    m, _ = mgr
    rec, token = m.register(["agent"], "/tmp")
    assert m.authenticate(rec.id, token)
    assert not m.authenticate(rec.id, token + "x")
    assert not m.authenticate("nope", token)
    assert not m.authenticate(rec.id, "")


def test_token_is_never_stored_in_plaintext(mgr):
    m, _ = mgr
    rec, token = m.register(["agent"], "/tmp")
    assert token not in rec.model_dump_json()
    assert all(token not in repr(s) for s in m._runs.values())


def test_tokens_are_unique_per_run(mgr):
    m, _ = mgr
    (r1, t1), (r2, t2) = m.register(["a"], "/"), m.register(["b"], "/")
    assert r1.id != r2.id and t1 != t2
    assert not m.authenticate(r1.id, t2)


def test_finish_revokes_token_and_records_exit(mgr):
    m, _ = mgr
    rec, token = m.register(["a"], "/")
    m.finish(rec.id, 0)
    view = m.get(rec.id)
    assert view.record.status == "finished" and view.record.exit_code == 0
    assert not m.authenticate(rec.id, token)


def test_unknown_run_raises(mgr):
    m, _ = mgr
    with pytest.raises(RunNotFound):
        m.get("missing")
    with pytest.raises(RunNotFound):
        m.finish("missing", 0)


def test_decision_counts_and_listing_order(mgr):
    m, _ = mgr
    first, _ = m.register(["a"], "/")
    second, _ = m.register(["b"], "/")
    m.record_decision(first.id, True)
    m.record_decision(first.id, False)
    m.record_decision(first.id, False)
    v = m.get(first.id)
    assert (v.allowed, v.blocked) == (1, 2)
    assert [x.record.id for x in m.list_runs()] == [second.id, first.id]


# --- stop -------------------------------------------------------------------

async def test_stop_kills_whole_process_tree_within_3_seconds(mgr):
    m, bus = mgr
    proc = subprocess.Popen(
        [sys.executable, "-c", TREE], start_new_session=True, stdout=subprocess.PIPE, text=True
    )
    threading.Thread(target=proc.wait, daemon=True).start()
    grandchild = int(proc.stdout.readline())
    assert alive(proc.pid) and alive(grandchild)

    rec, token = m.register(["tree"], "/")
    m.set_pid(rec.id, proc.pid)
    sub = bus.subscribe(run_id=rec.id)

    start = time.monotonic()
    assert await m.stop(rec.id, actor="tester", reason="demo") is True
    elapsed = time.monotonic() - start

    assert elapsed < 3.0
    assert wait_dead([proc.pid, grandchild])
    assert m.get(rec.id).record.status == "stopped"
    assert not m.authenticate(rec.id, token)  # revoked

    kinds = []
    while True:
        e = await sub.get()
        kinds.append(e.kind)
        if e.kind == "run_stopped":
            assert "tester" in e.reason and "demo" in e.reason
            break
    assert "run_started" not in kinds  # subscribed after registration


async def test_stop_escalates_to_sigkill_for_stubborn_process(mgr):
    m, _ = mgr
    proc = subprocess.Popen(
        [sys.executable, "-c", STUBBORN], start_new_session=True, stdout=subprocess.PIPE, text=True
    )
    threading.Thread(target=proc.wait, daemon=True).start()
    assert proc.stdout.readline().strip() == "ready"  # SIGTERM handler is installed

    rec, _ = m.register(["stubborn"], "/")
    m.set_pid(rec.id, proc.pid)
    start = time.monotonic()
    await m.stop(rec.id)
    assert time.monotonic() - start < 3.0
    assert wait_dead([proc.pid])


async def test_stop_is_idempotent_and_finish_after_stop_keeps_stopped(mgr):
    m, _ = mgr
    rec, _ = m.register(["x"], "/")
    assert await m.stop(rec.id) is True
    assert await m.stop(rec.id) is False
    m.finish(rec.id, -15)
    view = m.get(rec.id)
    assert view.record.status == "stopped" and view.record.exit_code == -15


async def test_stop_unknown_run_raises(mgr):
    m, _ = mgr
    with pytest.raises(RunNotFound):
        await m.stop("missing")


async def test_stop_with_already_dead_process_is_fine(mgr):
    m, _ = mgr
    proc = spawn("pass")
    proc.wait()
    rec, _ = m.register(["x"], "/")
    m.set_pid(rec.id, proc.pid)
    assert await m.stop(rec.id) is True


async def test_stop_persists_and_calls_on_run_end():
    ended = []
    store = SqliteStore(":memory:")
    m = RunManager(EventBus(), store, on_run_end=ended.append)
    rec, _ = m.register(["x"], "/")
    await m.stop(rec.id)
    assert store.get_run(rec.id).status == "stopped"
    assert ended == [rec.id]


async def test_never_signals_own_process_group():
    with pytest.raises(ValueError):
        await terminate_process_group(os.getpgrp())
    for bad in (0, 1, -5):
        with pytest.raises(ValueError):
            await terminate_process_group(bad)


async def test_pid_not_a_session_leader_only_kills_that_process(mgr):
    m, _ = mgr
    # Same process group as the test runner: must NOT killpg (would kill pytest).
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    threading.Thread(target=proc.wait, daemon=True).start()
    rec, _ = m.register(["x"], "/")
    m.set_pid(rec.id, proc.pid)
    await m.stop(rec.id)
    assert wait_dead([proc.pid])
    assert alive(os.getpid())
