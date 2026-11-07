"""mitmproxy addon: authenticate runs, enforce policy, emit events.

Flow for a managed agent:
  * HTTPS: the client sends `CONNECT host:443` with a Proxy-Authorization header.
    We authenticate it there and remember run_id for that client connection.
    The requests inside the intercepted tunnel carry no proxy credentials, so
    they are tied to the run through the connection, and checked against the
    run's live status (so a stopped run is cut off immediately).
  * Plain HTTP: every request carries Proxy-Authorization and is authenticated
    individually.
The Proxy-Authorization header is always stripped before forwarding upstream.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging

from mitmproxy import http

from .events import Event, EventBus
from .mcp import MAX_BODY_BYTES, tool_names
from .policy import PolicyEngine, RequestContext
from .redact import redact_headers, redact_path
from .runs import RunManager

log = logging.getLogger("harness.gateway")

UNAUTHENTICATED = "unauthenticated"
LOOPBACK = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "[::1]"}
STREAMING_TYPES = ("application/vnd.amazon.eventstream", "text/event-stream")


def parse_proxy_auth(header: str | None) -> tuple[str, str] | None:
    """Parse `Basic base64(run_id:token)`; None if absent or malformed."""
    if not header:
        return None
    scheme, _, value = header.strip().partition(" ")
    if scheme.lower() != "basic" or not value:
        return None
    try:
        decoded = base64.b64decode(value.strip(), validate=True).decode()
    except (binascii.Error, UnicodeDecodeError):
        return None
    run_id, sep, token = decoded.partition(":")
    if not sep or not run_id:
        return None
    return run_id, token


def _json_response(status: int, body: dict, extra: dict[str, str] | None = None) -> http.Response:
    headers = {"Content-Type": "application/json", "X-Harness-Blocked": "1", **(extra or {})}
    return http.Response.make(status, json.dumps(body).encode(), headers)


class HarnessGateway:
    def __init__(
        self,
        engine: PolicyEngine,
        runs: RunManager,
        bus: EventBus,
        protected_ports: set[int] | None = None,
    ) -> None:
        self.engine = engine
        self.runs = runs
        self.bus = bus
        # Ports of the harness itself (control API, the proxy). Agents may not reach them.
        self.protected_ports = set(protected_ports or ())
        self._conn_runs: dict[str, str] = {}  # client connection id -> run id
        self._tasks: set[asyncio.Task] = set()

    # --- helpers -------------------------------------------------------------

    def _is_protected(self, host: str, port: int) -> bool:
        return port in self.protected_ports and host.lower() in LOOPBACK

    def _emit(self, event: Event) -> None:
        self.bus.publish(event)

    def _reject_auth(self, flow: http.HTTPFlow, claimed_run: str | None, reason: str) -> None:
        flow.response = _json_response(
            407,
            {"blocked": True, "reason": reason},
            {"Proxy-Authenticate": 'Basic realm="agent-harness"'},
        )
        req = flow.request
        self._emit(
            Event(
                run_id=claimed_run or UNAUTHENTICATED,
                method=req.method,
                host=req.pretty_host,
                path=redact_path(req.path),
                decision="blocked",
                reason=reason,
                status_code=407,
            )
        )

    def _authenticate(self, flow: http.HTTPFlow) -> tuple[str | None, str | None, str]:
        """Return (run_id, claimed_run_id, failure_reason) for a proxy-addressed request."""
        creds = parse_proxy_auth(flow.request.headers.get("Proxy-Authorization"))
        if creds is None:
            return None, None, "proxy authentication required"
        run_id, token = creds
        if self.runs.authenticate(run_id, token):
            return run_id, run_id, ""
        return None, run_id, "invalid or revoked run credentials"

    # --- hooks ---------------------------------------------------------------

    def http_connect(self, flow: http.HTTPFlow) -> None:
        run_id, claimed, why = self._authenticate(flow)
        if run_id is None:
            self._reject_auth(flow, claimed, why)
            return
        self._conn_runs[flow.client_conn.id] = run_id
        # Never forward the credential to the upstream server.
        flow.request.headers.pop("Proxy-Authorization", None)

    def client_disconnected(self, client) -> None:
        self._conn_runs.pop(client.id, None)

    def request(self, flow: http.HTTPFlow) -> None:
        if flow.response is not None:  # already rejected (e.g. failed CONNECT)
            return
        req = flow.request

        # Resolve which run this request belongs to.
        run_id: str | None
        if "Proxy-Authorization" in req.headers:
            run_id, claimed, why = self._authenticate(flow)
            req.headers.pop("Proxy-Authorization", None)
            if run_id is None:
                self._reject_auth(flow, claimed, why)
                return
        else:
            run_id = self._conn_runs.get(flow.client_conn.id)
            if run_id is None:
                self._reject_auth(flow, None, "proxy authentication required")
                return

        host, port = req.pretty_host, req.port
        event = Event(
            run_id=run_id,
            method=req.method,
            host=host,
            path=redact_path(req.path),
            headers=redact_headers(dict(req.headers)),
        )
        flow.metadata["harness_event"] = event

        # The run may have been stopped while its tunnel is still open.
        if not self.runs.is_running(run_id):
            self._block(flow, event, None, "run is not active (stopped or finished)")
            return

        # Agents must not be able to reach the harness's own control plane.
        if self._is_protected(host, port):
            self._block(flow, event, "harness-protected", "access to the harness control plane is not allowed")
            return

        body = req.raw_content
        tools: tuple[str, ...] = ()
        if body and len(body) <= MAX_BODY_BYTES:
            tools = tool_names(req.get_content(strict=False))
        if tools:
            event.tool = ",".join(tools)

        decision = self.engine.evaluate(
            RequestContext(run_id=run_id, method=req.method, host=host, path=req.path, tools=tools)
        )
        event.rule_id, event.reason = decision.rule_id, decision.reason

        if decision.allowed:
            event.decision = "allowed"
            self.runs.record_decision(run_id, True)
            self._emit(event.model_copy())
            return

        self._block(flow, event, decision.rule_id, decision.reason)
        if decision.terminate:
            self._spawn_stop(run_id, decision.rule_id or "policy")

    def _block(self, flow: http.HTTPFlow, event: Event, rule_id: str | None, reason: str) -> None:
        event.decision = "blocked"
        event.rule_id = rule_id
        event.reason = reason
        event.status_code = 403
        self.runs.record_decision(event.run_id, False)
        flow.response = _json_response(403, {"blocked": True, "rule": rule_id, "reason": reason})
        self._emit(event.model_copy())

    def _spawn_stop(self, run_id: str, rule_id: str) -> None:
        task = asyncio.get_running_loop().create_task(
            self.runs.stop(run_id, actor="policy", reason=f"rule '{rule_id}' requires termination")
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def responseheaders(self, flow: http.HTTPFlow) -> None:
        if flow.response is None:
            return
        ctype = flow.response.headers.get("Content-Type", "").lower()
        if ctype.startswith(STREAMING_TYPES):
            flow.response.stream = True  # don't buffer model token streams

        event: Event | None = flow.metadata.get("harness_event")
        if event is not None and event.decision == "allowed":
            update = event.model_copy(update={"status_code": flow.response.status_code})
            self._emit(update)

    def error(self, flow: http.HTTPFlow) -> None:
        event: Event | None = flow.metadata.get("harness_event")
        if event is None or event.decision != "allowed" or flow.error is None:
            return
        self._emit(event.model_copy(update={"status_code": 502, "reason": f"upstream error: {flow.error.msg}"}))

    def tls_failed_client(self, data) -> None:
        run_id = self._conn_runs.get(data.context.client.id, UNAUTHENTICATED)
        address = data.context.server.address
        self._emit(
            Event(
                run_id=run_id,
                host=address[0] if address else None,
                decision="blocked",
                reason="TLS handshake failed: agent does not trust the harness CA",
            )
        )
