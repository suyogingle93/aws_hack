import asyncio
import os
import socket
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
import pytest_asyncio

from harness.events import EventBus
from harness.main import Backend, Settings
from harness.policy import PolicyEngine, PolicyError
from harness.watcher import watch_policy

POLICY = """\
default: deny
rules:
  - {id: allow-local, action: allow, host: 127.0.0.1, path: "/ok/*"}
"""


# --- policy watcher ---------------------------------------------------------------

async def test_watcher_reloads_on_change_and_announces(tmp_path):
    f = tmp_path / "p.yaml"
    f.write_text("default: deny\n")
    engine, bus = PolicyEngine.from_file(f), EventBus()
    sub = bus.subscribe()
    task = asyncio.create_task(watch_policy(engine, bus, interval=0.05))
    await asyncio.sleep(0.15)
    f.write_text("default: allow\n")
    ev = await asyncio.wait_for(sub.get(), 3)
    task.cancel()
    assert engine.version == 2 and engine.policy.default == "allow"
    assert ev.kind == "policy_reloaded" and "v2" in ev.reason


async def test_watcher_keeps_old_policy_on_invalid_edit(tmp_path):
    f = tmp_path / "p.yaml"
    f.write_text("default: allow\n")
    engine, bus = PolicyEngine.from_file(f), EventBus()
    sub = bus.subscribe()
    task = asyncio.create_task(watch_policy(engine, bus, interval=0.05))
    await asyncio.sleep(0.15)
    f.write_text("default: [oops")
    ev = await asyncio.wait_for(sub.get(), 3)
    task.cancel()
    assert engine.version == 1 and engine.policy.default == "allow"
    assert "previous policy" in ev.reason


async def test_watcher_survives_deleted_file(tmp_path):
    f = tmp_path / "p.yaml"
    f.write_text("default: allow\n")
    engine = PolicyEngine.from_file(f)
    task = asyncio.create_task(watch_policy(engine, EventBus(), interval=0.05))
    await asyncio.sleep(0.1)
    f.unlink()
    await asyncio.sleep(0.2)
    assert not task.done()
    task.cancel()


# --- backend lifecycle --------------------------------------------------------------

class Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self.server.seen.append(self.path)
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest_asyncio.fixture
async def backend(tmp_path):
    policy = tmp_path / "policy.yaml"
    policy.write_text(POLICY)
    up = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    up.seen = []
    threading.Thread(target=up.serve_forever, daemon=True).start()

    b = Backend(Settings(policy_path=policy, home=tmp_path / "home", proxy_port=0, api_port=0, watch_interval=0.05))
    await b.start()
    b.upstream = up
    yield b
    await b.stop()
    up.shutdown()


async def test_starts_serves_health_and_requires_auth(backend):
    base = f"http://127.0.0.1:{backend.api_port}"
    async with httpx.AsyncClient(trust_env=False) as c:
        assert (await c.get(f"{base}/health")).json()["ok"] is True
        assert (await c.get(f"{base}/runs")).status_code == 401
        ok = await c.get(f"{base}/runs", headers={"Authorization": f"Bearer {backend.operator_token}"})
        assert ok.status_code == 200 and ok.json() == []


async def test_token_file_is_owner_only_and_matches(backend):
    mode = stat.S_IMODE(os.stat(backend.token_file).st_mode)
    assert mode == 0o600
    assert backend.token_file.read_text() == backend.operator_token


async def test_register_response_points_at_the_live_proxy_and_ca(backend):
    async with httpx.AsyncClient(trust_env=False) as c:
        r = await c.post(
            f"http://127.0.0.1:{backend.api_port}/runs",
            json={"command": ["x"]},
            headers={"Authorization": f"Bearer {backend.operator_token}"},
        )
    body = r.json()
    assert body["proxy"]["port"] == backend.proxy_port
    assert os.path.exists(body["ca_path"])


async def test_end_to_end_allow_block_and_control_plane_protection(backend):
    rec, token = backend.runs.register(["agent"], "/")
    proxy = f"http://{rec.id}:{token}@127.0.0.1:{backend.proxy_port}"
    up = f"http://127.0.0.1:{backend.upstream.server_port}"
    async with httpx.AsyncClient(proxy=proxy, trust_env=False) as c:
        assert (await c.get(f"{up}/ok/1")).status_code == 200
        blocked = await c.get(f"{up}/nope")
        assert blocked.status_code == 403
        # The agent must not be able to reach the control API or the proxy itself:
        api = await c.get(f"http://127.0.0.1:{backend.api_port}/runs")
        assert api.status_code == 403 and "control plane" in api.json()["reason"]
        self_ = await c.get(f"http://127.0.0.1:{backend.proxy_port}/")
        assert self_.status_code == 403
    assert backend.upstream.seen == ["/ok/1"]


async def test_policy_file_edit_takes_effect_live(backend, tmp_path):
    rec, token = backend.runs.register(["agent"], "/")
    proxy = f"http://{rec.id}:{token}@127.0.0.1:{backend.proxy_port}"
    up = f"http://127.0.0.1:{backend.upstream.server_port}"
    async with httpx.AsyncClient(proxy=proxy, trust_env=False) as c:
        assert (await c.get(f"{up}/nope")).status_code == 403
        backend.settings.policy_path.write_text("default: allow\n")
        for _ in range(60):
            if (await c.get(f"{up}/nope")).status_code == 200:
                break
            await asyncio.sleep(0.05)
        else:
            pytest.fail("policy edit not applied")


async def test_shutdown_stops_running_agents_and_cleans_up(tmp_path):
    policy = tmp_path / "p.yaml"
    policy.write_text(POLICY)
    b = Backend(Settings(policy_path=policy, home=tmp_path / "home", proxy_port=0, api_port=0))
    await b.start()
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    threading.Thread(target=child.wait, daemon=True).start()
    rec, token = b.runs.register(["agent"], "/")
    b.runs.set_pid(rec.id, child.pid)

    await b.stop()

    assert b.runs.get(rec.id).record.status == "stopped"
    assert not b.runs.authenticate(rec.id, token)
    for _ in range(60):
        try:
            os.kill(child.pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("agent survived harness shutdown")
    assert not b.token_file.exists()
    await b.stop()  # second stop is a no-op


async def test_invalid_policy_fails_fast_before_starting_anything(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("default: [oops")
    b = Backend(Settings(policy_path=bad, home=tmp_path / "home", proxy_port=0, api_port=0))
    with pytest.raises(PolicyError):
        await b.start()
    assert not (tmp_path / "home" / "operator_token").exists()


async def test_port_in_use_fails_with_an_error(tmp_path):
    policy = tmp_path / "p.yaml"
    policy.write_text(POLICY)
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen()
    try:
        b = Backend(Settings(policy_path=policy, home=tmp_path / "home",
                             proxy_port=blocker.getsockname()[1], api_port=0))
        with pytest.raises(Exception):
            await asyncio.wait_for(b.start(), 15)
    finally:
        blocker.close()
