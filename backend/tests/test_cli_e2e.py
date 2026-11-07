"""End to end: the real `harness` CLI as a subprocess against an in-process backend."""

import asyncio
import json
import os
import signal
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import pytest_asyncio

from harness.main import Backend, Settings

BACKEND_DIR = Path(__file__).resolve().parents[1]

POLICY = """\
default: deny
rules:
  - {id: allow-ok, action: allow, host: 127.0.0.1, path: "/ok/*"}
  - {id: block-paste, action: deny, host: 127.0.0.1, path: "/paste/*", terminate_on_violation: true}
"""

AGENT = """\
import json, os, sys, urllib.request, urllib.error
base = sys.argv[1]
def get(p):
    try:
        with urllib.request.urlopen(base + p, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
print(json.dumps({
    "ok": get("/ok/1"), "blocked": get("/nope"),
    "operator_token_visible": any(k.startswith("HARNESS_OPERATOR") for k in os.environ),
    "run_id": os.environ.get("HARNESS_RUN_ID"), "proxy": os.environ.get("HTTP_PROXY"),
}), flush=True)
sys.exit(3)
"""

PASTE_AGENT = """\
import sys, time, urllib.request, urllib.error
try:
    urllib.request.urlopen(sys.argv[1] + "/paste/upload", timeout=10)
except urllib.error.HTTPError:
    pass
time.sleep(30)
print("SURVIVED", flush=True)
"""

# Signals readiness, then waits. On SIGINT it records the signal and exits 7.
WAITER = """\
import pathlib, signal, sys, time
ready = pathlib.Path(sys.argv[1])
def on_int(*a):
    pathlib.Path(sys.argv[1] + ".sigint").write_text("got it")
    sys.exit(7)
signal.signal(signal.SIGINT, on_int)
ready.write_text("ready")
time.sleep(60)
"""


class Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self.server.seen.append(self.path)
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


@pytest_asyncio.fixture
async def stack(tmp_path):
    policy = tmp_path / "policy.yaml"
    policy.write_text(POLICY)
    up = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    up.seen = []
    threading.Thread(target=up.serve_forever, daemon=True).start()
    backend = Backend(Settings(policy_path=policy, home=tmp_path / "home", proxy_port=0, api_port=0))
    await backend.start()

    s = type("S", (), {})()
    s.backend, s.upstream, s.tmp, s.seen = backend, f"http://127.0.0.1:{up.server_port}", tmp_path, up.seen
    s.env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
    s.env.update(
        PYTHONPATH=str(BACKEND_DIR),
        HARNESS_OPERATOR_TOKEN=backend.operator_token,
        HARNESS_API=f"http://127.0.0.1:{backend.api_port}",
    )

    async def cli(*args, env=None):
        return await asyncio.create_subprocess_exec(
            sys.executable, "-m", "harness", *args,
            env=env or s.env, cwd=BACKEND_DIR,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )

    s.cli = cli
    yield s
    await backend.stop()
    up.shutdown()


async def finish(proc, timeout=30):
    out, err = await asyncio.wait_for(proc.communicate(), timeout)
    return proc.returncode, out.decode(), err.decode()


async def wait_for_file(path, timeout=15):
    for _ in range(int(timeout / 0.05)):
        if Path(path).exists():
            return
        await asyncio.sleep(0.05)
    pytest.fail(f"{path} never appeared")


def only_run(stack):
    views = stack.backend.runs.list_runs()
    assert len(views) == 1
    return views[0]


# --- harness run ---------------------------------------------------------------------------

async def test_run_wires_agent_through_gateway_and_reports_result(stack):
    script = stack.tmp / "agent.py"
    script.write_text(AGENT)
    code, out, err = await finish(await stack.cli("run", "--", sys.executable, str(script), stack.upstream))

    assert code == 3  # the agent's own exit code is passed through
    seen = json.loads(out)
    assert seen["ok"] == 200 and seen["blocked"] == 403
    assert seen["operator_token_visible"] is False
    assert seen["run_id"] and seen["run_id"] in seen["proxy"]
    assert stack.seen == ["/ok/1"]  # the blocked request never reached the upstream

    view = only_run(stack)
    assert view.record.status == "finished" and view.record.exit_code == 3
    assert (view.allowed, view.blocked) == (1, 1)
    assert view.record.pid is not None and view.record.command[0] == sys.executable
    assert "finished (exit code 3)" in err


async def test_policy_violation_stops_the_run_and_kills_the_agent(stack):
    script = stack.tmp / "paste.py"
    script.write_text(PASTE_AGENT)
    code, out, err = await finish(await stack.cli("run", "--", sys.executable, str(script), stack.upstream))

    assert "SURVIVED" not in out
    assert code == 143  # SIGTERM from the harness
    view = only_run(stack)
    assert view.record.status == "stopped"
    assert "stopped by the harness" in err


async def test_stop_runs_and_tail_commands(stack):
    ready = stack.tmp / "ready"
    script = stack.tmp / "waiter.py"
    script.write_text(WAITER)
    agent = await stack.cli("run", "--", sys.executable, str(script), str(ready))
    await wait_for_file(ready)
    run_id = only_run(stack).record.id

    code, out, _ = await finish(await stack.cli("runs"))
    assert code == 0 and run_id in out and "running" in out

    tail = await stack.cli("tail", run_id, "--no-color")
    assert b"streaming" in await asyncio.wait_for(tail.stderr.readline(), 10)  # connected

    code, out, _ = await finish(await stack.cli("stop", run_id, "--reason", "demo"))
    assert code == 0 and f"stopped run {run_id}" in out

    tcode, tout, _ = await finish(tail)
    assert tcode == 0 and "RUN STOPPED" in tout and "demo" in tout  # tail exits when the run ends

    acode, _, aerr = await finish(agent)
    assert acode == 143 and "stopped by the harness" in aerr
    assert only_run(stack).record.status == "stopped"

    code, out, _ = await finish(await stack.cli("stop", run_id))  # idempotent
    assert code == 0 and "already stopped" in out


async def test_sigint_is_forwarded_to_the_agent(stack):
    ready = stack.tmp / "ready"
    script = stack.tmp / "waiter.py"
    script.write_text(WAITER)
    agent = await stack.cli("run", "--", sys.executable, str(script), str(ready))
    await wait_for_file(ready)

    agent.send_signal(signal.SIGINT)  # as if the user pressed Ctrl+C in the launcher
    code, _, _ = await finish(agent)

    assert code == 7  # the agent handled it and chose its own exit code
    assert Path(str(ready) + ".sigint").read_text() == "got it"
    assert only_run(stack).record.status == "finished"


# --- failure modes ----------------------------------------------------------------------------

async def test_backend_unreachable_refuses_to_start_the_agent(stack):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    dead = s.getsockname()[1]
    s.close()
    marker = stack.tmp / "ran"
    code, _, err = await finish(await stack.cli(
        "run", "--", sys.executable, "-c", f"open({str(marker)!r}, 'w')",
        env={**stack.env, "HARNESS_API": f"http://127.0.0.1:{dead}"},
    ))
    assert code == 2 and "harness serve" in err
    assert not marker.exists()
    assert stack.backend.runs.list_runs() == []


async def test_wrong_operator_token_is_reported(stack):
    code, _, err = await finish(await stack.cli(
        "run", "--", sys.executable, "-c", "pass", env={**stack.env, "HARNESS_OPERATOR_TOKEN": "wrong"}))
    assert code == 2 and "rejected the operator token" in err
    assert stack.backend.runs.list_runs() == []


async def test_command_not_found_reports_127_and_closes_the_run(stack):
    code, _, err = await finish(await stack.cli("run", "--", "definitely-not-a-real-command-xyz"))
    assert code == 127 and "cannot start" in err
    view = only_run(stack)  # the registered run must not be left dangling as "running"
    assert view.record.status == "finished" and view.record.exit_code == 127
    assert not stack.backend.runs.authenticate(view.record.id, "anything")


async def test_stop_unknown_run_fails_cleanly(stack):
    code, _, err = await finish(await stack.cli("stop", "nope"))
    assert code == 2 and "404" in err
