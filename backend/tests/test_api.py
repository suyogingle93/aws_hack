"""Control API tests against a real uvicorn server (so the WebSocket shares the bus's event loop)."""

import asyncio
import json
import os
import subprocess
import sys
import threading

import httpx
import pytest
import pytest_asyncio
import uvicorn
import websockets

from harness.api import ApiConfig, create_app
from harness.events import Event, EventBus
from harness.policy import PolicyEngine, parse_policy
from harness.runs import RunManager
from harness.store import SqliteStore, StoreWriter

OP = "operator-secret-token"
POLICY = {"default": "deny", "rules": [{"id": "a", "action": "allow", "host": "x.com"}]}


class Env:
    pass


@pytest_asyncio.fixture
async def env(tmp_path):
    e = Env()
    e.bus = EventBus()
    e.store = SqliteStore(":memory:")
    e.runs = RunManager(e.bus, e.store, grace=0.5)
    e.policy_file = tmp_path / "policy.yaml"
    e.policy_file.write_text("default: deny\nrules:\n  - {id: a, action: allow, host: x.com}\n")
    e.engine = PolicyEngine.from_file(e.policy_file)
    e.writer = StoreWriter(e.bus, e.store)
    e.writer.start()

    app = create_app(ApiConfig(OP, proxy_port=4321, ca_path="/tmp/ca.pem"), e.runs, e.engine, e.bus, e.store)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    e.base, e.ws_base = f"http://127.0.0.1:{port}", f"ws://127.0.0.1:{port}"
    e.http = httpx.AsyncClient(base_url=e.base, headers={"Authorization": f"Bearer {OP}"}, trust_env=False)
    yield e
    await e.http.aclose()
    server.should_exit = True
    await task
    await e.writer.stop()


async def register(env, cmd=("agent",)):
    r = await env.http.post("/runs", json={"command": list(cmd), "cwd": "/tmp"})
    assert r.status_code == 201
    return r.json()


# --- auth ------------------------------------------------------------------------

async def test_health_is_public(env):
    async with httpx.AsyncClient(base_url=env.base, trust_env=False) as c:
        r = await c.get("/health")
    assert r.status_code == 200 and r.json()["ok"] is True


@pytest.mark.parametrize(
    "method,path",
    [("GET", "/runs"), ("POST", "/runs"), ("GET", "/runs/abc"), ("POST", "/runs/abc/stop"),
     ("GET", "/policy"), ("POST", "/policy/reload"), ("PUT", "/policy"), ("GET", "/runs/abc/events")],
)
async def test_every_other_route_requires_the_operator_token(env, method, path):
    async with httpx.AsyncClient(base_url=env.base, trust_env=False) as c:
        none = await c.request(method, path, json={})
        wrong = await c.request(method, path, json={}, headers={"Authorization": "Bearer nope"})
    assert none.status_code == 401 and wrong.status_code == 401


async def test_run_token_is_not_an_operator_token(env):
    reg = await register(env)
    async with httpx.AsyncClient(base_url=env.base, trust_env=False) as c:
        r = await c.get("/runs", headers={"Authorization": f"Bearer {reg['token']}"})
    assert r.status_code == 401


# --- runs ------------------------------------------------------------------------

async def test_register_returns_credentials_and_connection_info(env):
    reg = await register(env)
    assert reg["run_id"] and reg["token"]
    assert reg["proxy"] == {"host": "127.0.0.1", "port": 4321} and reg["ca_path"] == "/tmp/ca.pem"
    assert env.runs.authenticate(reg["run_id"], reg["token"])


async def test_register_validates_body(env):
    assert (await env.http.post("/runs", json={"command": []})).status_code == 422
    assert (await env.http.post("/runs", json={})).status_code == 422


async def test_list_get_finish_with_counts(env):
    reg = await register(env)
    rid = reg["run_id"]
    env.runs.record_decision(rid, True)
    env.runs.record_decision(rid, False)
    listed = (await env.http.get("/runs")).json()
    assert listed[0]["id"] == rid and (listed[0]["allowed"], listed[0]["blocked"]) == (1, 1)
    assert "token" not in json.dumps(listed)

    fin = await env.http.post(f"/runs/{rid}/finish", json={"exit_code": 0})
    assert fin.json()["status"] == "finished" and fin.json()["exit_code"] == 0
    assert (await env.http.get(f"/runs/{rid}")).json()["status"] == "finished"


async def test_unknown_run_is_404(env):
    for method, path, body in [
        ("GET", "/runs/nope", None), ("POST", "/runs/nope/stop", {}), ("GET", "/runs/nope/events", None),
        ("POST", "/runs/nope/finish", {"exit_code": 0}), ("POST", "/runs/nope/pid", {"pid": 99999}),
    ]:
        assert (await env.http.request(method, path, json=body)).status_code == 404


async def test_pid_must_be_sane(env):
    reg = await register(env)
    for bad in (0, 1, -1):
        r = await env.http.post(f"/runs/{reg['run_id']}/pid", json={"pid": bad})
        assert r.status_code == 422


async def test_stop_kills_process_and_revokes_credentials(env):
    reg = await register(env)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    threading.Thread(target=child.wait, daemon=True).start()
    assert (await env.http.post(f"/runs/{reg['run_id']}/pid", json={"pid": child.pid})).status_code == 200

    r = await env.http.post(f"/runs/{reg['run_id']}/stop", json={"reason": "demo"})
    assert r.json()["stopped"] is True and r.json()["run"]["status"] == "stopped"
    assert not env.runs.authenticate(reg["run_id"], reg["token"])
    for _ in range(60):
        try:
            os.kill(child.pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("process survived stop")

    again = await env.http.post(f"/runs/{reg['run_id']}/stop")  # no body is fine, and idempotent
    assert again.status_code == 200 and again.json()["stopped"] is False


async def test_events_endpoint_returns_persisted_events(env):
    reg = await register(env)
    env.bus.publish(Event(run_id=reg["run_id"], host="x.com", decision="allowed"))
    for _ in range(40):  # writer is asynchronous
        r = await env.http.get(f"/runs/{reg['run_id']}/events")
        kinds = [e["kind"] for e in r.json()]
        if "request" in kinds:
            break
        await asyncio.sleep(0.05)
    assert kinds == ["run_started", "request"]
    assert (await env.http.get(f"/runs/{reg['run_id']}/events", params={"limit": 0})).status_code == 422


# --- policy ----------------------------------------------------------------------

async def test_get_policy(env):
    body = (await env.http.get("/policy")).json()
    assert body["version"] == 1 and body["policy"]["default"] == "deny"
    assert body["policy"]["rules"][0]["id"] == "a"


async def test_reload_applies_file_changes_and_announces(env):
    sub = env.bus.subscribe()
    env.policy_file.write_text("default: allow\n")
    r = await env.http.post("/policy/reload")
    assert r.json() == {"version": 2}
    assert (await env.http.get("/policy")).json()["policy"]["default"] == "allow"
    ev = await asyncio.wait_for(sub.get(), 2)
    assert ev.kind == "policy_reloaded" and "v2" in ev.reason


async def test_invalid_reload_is_rejected_and_old_policy_kept(env):
    env.policy_file.write_text("default: [oops")
    r = await env.http.post("/policy/reload")
    assert r.status_code == 400 and "previous policy kept" in r.json()["detail"]
    assert (await env.http.get("/policy")).json()["version"] == 1


async def test_put_policy_validates_and_replaces(env):
    ok = await env.http.put("/policy", json={"default": "allow"})
    assert ok.json() == {"version": 2}
    bad = await env.http.put("/policy", json={"default": "maybe"})
    assert bad.status_code == 400
    dup = await env.http.put("/policy", json={"rules": [
        {"id": "z", "action": "allow", "host": "a"}, {"id": "z", "action": "deny", "host": "b"}]})
    assert dup.status_code == 400
    assert (await env.http.get("/policy")).json()["version"] == 2


# --- websocket -------------------------------------------------------------------

async def test_ws_streams_live_events_with_run_filter(env):
    async with websockets.connect(f"{env.ws_base}/ws?run_id=r1&token={OP}") as ws:
        for _ in range(100):  # wait for the server-side subscription
            if env.bus.subscriber_count >= 2:  # store writer + this socket
                break
            await asyncio.sleep(0.02)
        env.bus.publish(Event(run_id="r2", host="other.com"))
        env.bus.publish(Event(run_id="r1", host="mine.com", decision="blocked"))
        msg = json.loads(await asyncio.wait_for(ws.recv(), 3))
    assert msg["host"] == "mine.com" and msg["decision"] == "blocked"


async def test_ws_header_auth_also_works(env):
    async with websockets.connect(f"{env.ws_base}/ws", additional_headers={"Authorization": f"Bearer {OP}"}) as ws:
        for _ in range(100):
            if env.bus.subscriber_count >= 2:
                break
            await asyncio.sleep(0.02)
        env.bus.publish(Event(run_id="r1", host="h.com"))
        assert json.loads(await asyncio.wait_for(ws.recv(), 3))["host"] == "h.com"


@pytest.mark.parametrize("query", ["", "?token=wrong"])
async def test_ws_rejects_bad_auth(env, query):
    with pytest.raises(websockets.exceptions.InvalidStatus):
        async with websockets.connect(f"{env.ws_base}/ws{query}"):
            pass


async def test_ws_disconnect_unsubscribes(env):
    base = env.bus.subscriber_count
    async with websockets.connect(f"{env.ws_base}/ws?token={OP}"):
        for _ in range(100):
            if env.bus.subscriber_count > base:
                break
            await asyncio.sleep(0.02)
        assert env.bus.subscriber_count == base + 1
    for _ in range(100):  # disconnect is noticed on the next send
        env.bus.publish(Event(run_id="r1"))
        if env.bus.subscriber_count == base:
            break
        await asyncio.sleep(0.05)
    assert env.bus.subscriber_count == base
