"""Integration tests: a real mitmproxy instance, real HTTP/HTTPS stub upstreams, real clients."""

import asyncio
import datetime
import ipaddress
import json
import os
import socket
import ssl
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
import pytest_asyncio
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from mitmproxy import options
from mitmproxy.tools.dump import DumpMaster

from harness.events import EventBus
from harness.gateway import HarnessGateway
from harness.policy import PolicyEngine, parse_policy
from harness.runs import RunManager

POLICY = {
    "default": "deny",
    "rules": [
        {"id": "allow-ok", "action": "allow", "host": "127.0.0.1", "path": "/ok/*"},
        {"id": "allow-mcp", "action": "allow", "host": "127.0.0.1", "path": "/mcp", "methods": ["POST"]},
        {"id": "block-tools", "action": "deny", "mcp_tool": "delete_*"},
        {"id": "block-paste", "action": "deny", "host": "127.0.0.1", "path": "/paste/*", "terminate_on_violation": True},
    ],
}


# --- stub upstream servers -----------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _reply(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self.server.seen.append({"method": self.command, "path": self.path, "headers": dict(self.headers), "body": body})
        out = f"ok:{self.path}".encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    do_GET = do_POST = _reply

    def log_message(self, *a):
        pass


def make_cert(path_cert, path_key):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .sign(key, hashes.SHA256())
    )
    path_cert.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    path_key.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption())
    )


def start_server(tls_files=None):
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.seen = []
    if tls_files:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(*tls_files)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# --- harness fixture -----------------------------------------------------------

class Env:
    pass


@pytest_asyncio.fixture
async def env(tmp_path):
    e = Env()
    cert, key = tmp_path / "up.pem", tmp_path / "up.key"
    make_cert(cert, key)
    e.http_up, e.https_up = start_server(), start_server((cert, key))
    e.http_port, e.https_port = e.http_up.server_port, e.https_up.server_port

    e.bus = EventBus()
    e.runs = RunManager(e.bus, grace=0.5)
    e.engine = PolicyEngine(policy=parse_policy(POLICY))
    e.gateway = HarnessGateway(e.engine, e.runs, e.bus)

    confdir = tmp_path / "mitm"
    opts = options.Options(listen_host="127.0.0.1", listen_port=0, confdir=str(confdir), ssl_insecure=True)
    master = DumpMaster(opts, with_termlog=False, with_dumper=False)
    master.addons.add(e.gateway)
    task = asyncio.create_task(master.run())

    proxyserver = master.addons.get("proxyserver")
    for _ in range(200):
        if proxyserver.listen_addrs() and (confdir / "mitmproxy-ca-cert.pem").exists():
            break
        await asyncio.sleep(0.05)
    else:
        raise RuntimeError("mitmproxy did not start")
    e.proxy_port = proxyserver.listen_addrs()[0][1]
    e.ca = str(confdir / "mitmproxy-ca-cert.pem")
    e.sub = e.bus.subscribe()

    def client(run_id, token):
        auth = f"{run_id}:{token}@" if run_id is not None else ""
        return httpx.AsyncClient(proxy=f"http://{auth}127.0.0.1:{e.proxy_port}", verify=e.ca, trust_env=False, timeout=10)

    e.client = client
    e.register = lambda cmd=("agent",): e.runs.register(list(cmd), "/")

    yield e

    master.shutdown()
    await asyncio.wait_for(task, 10)
    e.http_up.shutdown()
    e.https_up.shutdown()


async def events_until(sub, pred, timeout=5.0):
    """Collect events until one satisfies pred; return all collected."""
    got = []
    async def run():
        while True:
            ev = await sub.get()
            got.append(ev)
            if pred(ev):
                return
    await asyncio.wait_for(run(), timeout)
    return got


def request_events(sub_events):
    return [e for e in sub_events if e.kind == "request"]


# --- allowed traffic -----------------------------------------------------------

async def test_http_allowed_and_credential_not_forwarded(env):
    rec, token = env.register()
    async with env.client(rec.id, token) as c:
        r = await c.get(f"http://127.0.0.1:{env.http_port}/ok/1")
    assert r.status_code == 200 and r.text == "ok:/ok/1"
    seen = env.http_up.seen[0]
    assert "Proxy-Authorization" not in seen["headers"]  # never leak the run token upstream

    evs = await events_until(env.sub, lambda e: e.status_code == 200)
    ev = request_events(evs)[-1]
    assert ev.decision == "allowed" and ev.rule_id == "allow-ok" and ev.run_id == rec.id
    assert env.runs.get(rec.id).allowed == 1


async def test_https_allowed_through_intercepted_tunnel(env):
    rec, token = env.register()
    async with env.client(rec.id, token) as c:
        r = await c.get(f"https://127.0.0.1:{env.https_port}/ok/secure")
    assert r.status_code == 200 and r.text == "ok:/ok/secure"
    assert "Proxy-Authorization" not in env.https_up.seen[0]["headers"]
    evs = await events_until(env.sub, lambda e: e.status_code == 200)
    assert request_events(evs)[-1].decision == "allowed"


# --- blocked traffic -----------------------------------------------------------

async def test_https_blocked_returns_403_and_never_reaches_upstream(env):
    rec, token = env.register()
    async with env.client(rec.id, token) as c:
        r = await c.get(f"https://127.0.0.1:{env.https_port}/secret")
    assert r.status_code == 403
    body = r.json()
    assert body["blocked"] is True and "default" in body["reason"]
    assert r.headers["X-Harness-Blocked"] == "1"
    assert env.https_up.seen == []
    evs = await events_until(env.sub, lambda e: e.decision == "blocked")
    assert request_events(evs)[-1].status_code == 403
    assert env.runs.get(rec.id).blocked == 1


async def test_mcp_destructive_tool_blocked_but_safe_tool_allowed(env):
    rec, token = env.register()
    url = f"http://127.0.0.1:{env.http_port}/mcp"

    def rpc(name):
        return {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": {}}}

    async with env.client(rec.id, token) as c:
        bad = await c.post(url, json=rpc("delete_repo"))
        good = await c.post(url, json=rpc("list_files"))
    assert bad.status_code == 403 and bad.json()["rule"] == "block-tools"
    assert good.status_code == 200
    assert len(env.http_up.seen) == 1  # only the safe call reached upstream
    evs = await events_until(env.sub, lambda e: e.status_code == 200)
    tools = {e.tool for e in request_events(evs)}
    assert {"delete_repo", "list_files"} <= tools


# --- authentication ------------------------------------------------------------

async def test_http_without_credentials_gets_407(env):
    async with env.client(None, None) as c:
        r = await c.get(f"http://127.0.0.1:{env.http_port}/ok/1")
    assert r.status_code == 407 and "Proxy-Authenticate" in r.headers
    assert env.http_up.seen == []


async def test_https_without_credentials_is_refused(env):
    async with env.client(None, None) as c:
        with pytest.raises(httpx.ProxyError):
            await c.get(f"https://127.0.0.1:{env.https_port}/ok/1")
    assert env.https_up.seen == []


async def test_wrong_token_rejected_and_logged_under_claimed_run(env):
    rec, _ = env.register()
    async with env.client(rec.id, "wrong") as c:
        r = await c.get(f"http://127.0.0.1:{env.http_port}/ok/1")
    assert r.status_code == 407
    evs = await events_until(env.sub, lambda e: e.status_code == 407)
    assert request_events(evs)[-1].run_id == rec.id


# --- the kill switch, end to end -----------------------------------------------

async def test_stopped_run_is_cut_off_even_on_open_tunnel_and_for_new_connections(env):
    rec, token = env.register()
    async with env.client(rec.id, token) as c:
        ok = await c.get(f"https://127.0.0.1:{env.https_port}/ok/1")
        assert ok.status_code == 200
        await env.runs.stop(rec.id, actor="test", reason="mid-task revoke")
        # Same keep-alive tunnel, run now stopped:
        after = await c.get(f"https://127.0.0.1:{env.https_port}/ok/2")
    assert after.status_code == 403 and "not active" in after.json()["reason"]
    assert len(env.https_up.seen) == 1  # second request never reached upstream

    # A brand-new connection can no longer authenticate:
    async with env.client(rec.id, token) as c2:
        r = await c2.get(f"http://127.0.0.1:{env.http_port}/ok/3")
    assert r.status_code == 407


async def test_terminate_on_violation_stops_the_run_and_kills_the_agent(env):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    threading.Thread(target=child.wait, daemon=True).start()
    rec, token = env.register(["agent"])
    env.runs.set_pid(rec.id, child.pid)

    async with env.client(rec.id, token) as c:
        r = await c.get(f"https://127.0.0.1:{env.https_port}/paste/upload")
    assert r.status_code == 403 and r.json()["rule"] == "block-paste"

    evs = await events_until(env.sub, lambda e: e.kind == "run_stopped")
    assert "block-paste" in evs[-1].reason and "policy" in evs[-1].reason
    for _ in range(60):
        if env.runs.get(rec.id).record.status == "stopped":
            break
        await asyncio.sleep(0.05)
    assert env.runs.get(rec.id).record.status == "stopped"
    for _ in range(60):
        try:
            os.kill(child.pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("agent process still alive after policy termination")


# --- control plane protection ---------------------------------------------------

async def test_agent_cannot_reach_control_plane(env):
    env.gateway.protected_ports.add(env.http_port)  # pretend the upstream is the control API
    rec, token = env.register()
    async with env.client(rec.id, token) as c:
        r = await c.get(f"http://127.0.0.1:{env.http_port}/ok/1")
    assert r.status_code == 403 and "control plane" in r.json()["reason"]
    assert env.http_up.seen == []


# --- upstream failures ----------------------------------------------------------

async def test_upstream_connection_failure_is_logged_as_502(env):
    rec, token = env.register()
    dead = free_port()
    async with env.client(rec.id, token) as c:
        r = await c.get(f"http://127.0.0.1:{dead}/ok/1")
    assert r.status_code == 502
    evs = await events_until(env.sub, lambda e: e.status_code == 502)
    assert "upstream error" in request_events(evs)[-1].reason


# --- redaction in emitted events -------------------------------------------------

async def test_emitted_events_do_not_contain_secrets(env):
    rec, token = env.register()
    async with env.client(rec.id, token) as c:
        await c.get(
            f"http://127.0.0.1:{env.http_port}/ok/1?token=supersecret",
            headers={"X-Api-Key": "k-12345", "Authorization": "Bearer abcdefghijkl"},
        )
    evs = await events_until(env.sub, lambda e: e.status_code == 200)
    dump = json.dumps([e.model_dump(mode="json") for e in evs])
    for secret in ("supersecret", "k-12345", "abcdefghijkl", token):
        assert secret not in dump
