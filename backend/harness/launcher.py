"""Launch an agent under the harness: register a run, wire its network through the gateway,
supervise the process, and report how it ended."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import httpx

DEFAULT_API = "http://127.0.0.1:8000"
TOKEN_ENV = "HARNESS_OPERATOR_TOKEN"
HOME_ENV = "HARNESS_HOME"
API_ENV = "HARNESS_API"

# Never hand the agent the keys to the harness.
_STRIPPED_FROM_CHILD = {TOKEN_ENV, HOME_ENV, API_ENV}
_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
_CA_VARS = (
    "SSL_CERT_FILE",  # Python ssl, OpenSSL, curl on some builds
    "REQUESTS_CA_BUNDLE",  # requests
    "CURL_CA_BUNDLE",  # curl, older requests
    "NODE_EXTRA_CA_CERTS",  # Node.js
    "AWS_CA_BUNDLE",  # boto3 / AWS CLI
    "GIT_SSL_CAINFO",  # git
)


class LauncherError(Exception):
    def __init__(self, message: str, exit_code: int = 2) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class RunInfo:
    run_id: str
    token: str
    proxy_host: str
    proxy_port: int
    ca_path: str | None

    @property
    def proxy_url(self) -> str:
        # Token alphabet is URL-safe, so no escaping is needed in the userinfo part.
        return f"http://{self.run_id}:{self.token}@{self.proxy_host}:{self.proxy_port}"


def resolve_operator_token(env: Mapping[str, str], home: Path | None = None) -> str:
    token = env.get(TOKEN_ENV)
    if token:
        return token.strip()
    home = home or Path(env.get(HOME_ENV) or Path.home() / ".agent-harness")
    token_file = home / "operator_token"
    try:
        return token_file.read_text().strip()
    except OSError:
        raise LauncherError(
            f"no operator token: set {TOKEN_ENV} or start the backend (`harness serve`), "
            f"which writes {token_file}"
        ) from None


def build_child_env(base: Mapping[str, str], info: RunInfo) -> dict[str, str]:
    """Environment for the agent: all traffic through the gateway, gateway CA trusted."""
    if not info.ca_path:
        raise LauncherError("backend did not provide a CA certificate path")
    env = {k: v for k, v in base.items() if k not in _STRIPPED_FROM_CHILD}
    for name in _PROXY_VARS:  # drop any inherited settings, either case
        env.pop(name, None)
        env.pop(name.lower(), None)
    url = info.proxy_url
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        env[name] = url
        env[name.lower()] = url
    # Nothing is exempt from the proxy, not even localhost (the gateway blocks the control plane).
    env["NO_PROXY"] = env["no_proxy"] = ""
    for name in _CA_VARS:
        env[name] = info.ca_path
    env["HARNESS_RUN_ID"] = info.run_id
    return env


def exit_code_for(returncode: int) -> int:
    """Shell convention: killed by signal N -> 128+N."""
    return 128 + -returncode if returncode < 0 else returncode


class HarnessClient:
    def __init__(self, api_url: str, token: str, timeout: float = 10.0) -> None:
        self.api_url = api_url.rstrip("/")
        # trust_env=False: our own API calls must never be routed through a user/system proxy.
        self._http = httpx.Client(
            base_url=self.api_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            trust_env=False,
        )

    def close(self) -> None:
        self._http.close()

    def _call(self, method: str, path: str, **kw: Any) -> Any:
        try:
            r = self._http.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise LauncherError(
                f"cannot reach the harness backend at {self.api_url} ({type(e).__name__}). "
                "Start it with: harness serve"
            ) from None
        if r.status_code == 401:
            raise LauncherError("the harness backend rejected the operator token")
        if r.status_code >= 400:
            detail = r.text[:200]
            raise LauncherError(f"{method} {path} failed: {r.status_code} {detail}")
        return r.json()

    def health(self) -> dict:
        try:
            r = self._http.get("/health")
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError):
            raise LauncherError(
                f"cannot reach the harness backend at {self.api_url}. Start it with: harness serve"
            ) from None

    def register(self, command: Sequence[str], cwd: str) -> RunInfo:
        d = self._call("POST", "/runs", json={"command": list(command), "cwd": cwd})
        return RunInfo(d["run_id"], d["token"], d["proxy"]["host"], d["proxy"]["port"], d.get("ca_path"))

    def set_pid(self, run_id: str, pid: int) -> None:
        self._call("POST", f"/runs/{run_id}/pid", json={"pid": pid})

    def finish(self, run_id: str, exit_code: int) -> dict:
        return self._call("POST", f"/runs/{run_id}/finish", json={"exit_code": exit_code})

    def stop(self, run_id: str, reason: str = "stopped by operator") -> dict:
        return self._call("POST", f"/runs/{run_id}/stop", json={"reason": reason})

    def runs(self) -> list[dict]:
        return self._call("GET", "/runs")


def _log(msg: str) -> None:
    print(f"[harness] {msg}", file=sys.stderr, flush=True)


def _kill_group(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)  # the child is a session leader, so pgid == pid
    except ProcessLookupError:
        pass


def run_agent(
    client: HarnessClient,
    command: Sequence[str],
    cwd: str | None = None,
    base_env: Mapping[str, str] | None = None,
    log: Callable[[str], None] = _log,
) -> int:
    """Run `command` under the harness and return the exit code to use."""
    cwd = cwd or os.getcwd()
    client.health()  # fail fast, before starting anything
    info = client.register(command, cwd)
    env = build_child_env(base_env if base_env is not None else os.environ, info)

    try:
        proc = subprocess.Popen(list(command), env=env, cwd=cwd, start_new_session=True)
    except OSError as e:
        _finish_quietly(client, info.run_id, 127, log)
        raise LauncherError(f"cannot start {command[0]!r}: {e}", exit_code=127) from None

    try:
        client.set_pid(info.run_id, proc.pid)
    except LauncherError:
        # Without a pid the harness could not kill this agent later. Fail closed.
        _kill_group(proc.pid, signal.SIGKILL)
        proc.wait()
        _finish_quietly(client, info.run_id, -9, log)
        raise LauncherError("could not register the agent's pid with the harness; agent terminated") from None

    log(f"run {info.run_id} started: {' '.join(command)}")

    # Forward termination signals to the agent's whole process group; a second one force-kills.
    previous: dict[int, Any] = {}
    if threading.current_thread() is threading.main_thread():
        count = {"n": 0}

        def forward(signum: int, _frame: Any) -> None:
            count["n"] += 1
            _kill_group(proc.pid, signum if count["n"] == 1 else signal.SIGKILL)

        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous[sig] = signal.signal(sig, forward)

    try:
        while True:
            try:
                returncode = proc.wait()
                break
            except KeyboardInterrupt:  # signal arrived while handlers were being swapped
                continue
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)

    code = exit_code_for(returncode)
    try:
        result = client.finish(info.run_id, returncode)
        if result.get("status") == "stopped":
            log(f"run {info.run_id} was stopped by the harness (exit code {code})")
        else:
            log(f"run {info.run_id} finished (exit code {code})")
    except LauncherError as e:
        log(f"warning: could not report run result: {e}")
    return code


def _finish_quietly(client: HarnessClient, run_id: str, code: int, log: Callable[[str], None]) -> None:
    try:
        client.finish(run_id, code)
    except LauncherError as e:
        log(f"warning: could not report run result: {e}")
