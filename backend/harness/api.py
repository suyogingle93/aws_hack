"""Control API: runs, events, policy, kill switch, live event stream.

Everything except /health requires the operator token. The gateway denies
managed agents any access to this API's port.
"""

from __future__ import annotations

import asyncio
import hmac
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from .events import Event, EventBus
from .policy import Policy, PolicyEngine, PolicyError, parse_policy
from .runs import RunManager, RunNotFound, RunView
from .store.base import AuditStore

WS_UNAUTHORIZED = 4401


@dataclass(frozen=True)
class ApiConfig:
    operator_token: str
    proxy_host: str = "127.0.0.1"
    proxy_port: int = 8080
    ca_path: str | None = None  # mitmproxy CA the agent must trust
    cors_origins: tuple[str, ...] = ("http://localhost:5173", "http://127.0.0.1:5173")


class RegisterRun(BaseModel):
    command: list[str] = Field(min_length=1)
    cwd: str = "."


class PidBody(BaseModel):
    pid: int = Field(gt=1)


class FinishBody(BaseModel):
    exit_code: int


class StopBody(BaseModel):
    reason: str = "stopped by operator"


def _run_json(view: RunView) -> dict[str, Any]:
    data = view.record.model_dump(mode="json")
    data["allowed"], data["blocked"] = view.allowed, view.blocked
    return data


def create_app(
    config: ApiConfig,
    runs: RunManager,
    engine: PolicyEngine,
    bus: EventBus,
    store: AuditStore | None = None,
) -> FastAPI:
    app = FastAPI(title="Agent Harness control API")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(config.cors_origins),
        allow_methods=["*"],
        allow_headers=["Authorization", "Content-Type"],
    )
    bearer = HTTPBearer(auto_error=False)

    def token_ok(candidate: str | None) -> bool:
        return bool(candidate) and hmac.compare_digest(candidate.encode(), config.operator_token.encode())

    def require_operator(creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> None:
        if creds is None or not token_ok(creds.credentials):
            raise HTTPException(401, "operator token required", headers={"WWW-Authenticate": "Bearer"})

    auth = [Depends(require_operator)]

    def run_or_404(run_id: str) -> RunView:
        try:
            return runs.get(run_id)
        except RunNotFound:
            raise HTTPException(404, f"unknown run '{run_id}'") from None

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "policy_version": engine.version}

    # --- runs ------------------------------------------------------------------

    @app.post("/runs", dependencies=auth, status_code=201)
    def register_run(body: RegisterRun) -> dict[str, Any]:
        record, token = runs.register(body.command, body.cwd)
        return {
            "run_id": record.id,
            "token": token,  # shown once; only a hash is kept server-side
            "proxy": {"host": config.proxy_host, "port": config.proxy_port},
            "ca_path": config.ca_path,
        }

    @app.post("/runs/{run_id}/pid", dependencies=auth)
    def set_pid(run_id: str, body: PidBody) -> dict[str, Any]:
        run_or_404(run_id)
        runs.set_pid(run_id, body.pid)
        return {"ok": True}

    @app.post("/runs/{run_id}/finish", dependencies=auth)
    def finish_run(run_id: str, body: FinishBody) -> dict[str, Any]:
        run_or_404(run_id)
        runs.finish(run_id, body.exit_code)
        return _run_json(runs.get(run_id))

    @app.get("/runs", dependencies=auth)
    def list_runs() -> list[dict[str, Any]]:
        return [_run_json(v) for v in runs.list_runs()]

    @app.get("/runs/{run_id}", dependencies=auth)
    def get_run(run_id: str) -> dict[str, Any]:
        return _run_json(run_or_404(run_id))

    @app.get("/runs/{run_id}/events", dependencies=auth)
    def run_events(
        run_id: str,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = Query(1000, ge=1, le=10_000),
    ) -> list[dict[str, Any]]:
        run_or_404(run_id)
        if store is None:
            return []
        return [e.model_dump(mode="json") for e in store.list_events(run_id, since, until, limit)]

    @app.post("/runs/{run_id}/stop", dependencies=auth)
    async def stop_run(run_id: str, body: StopBody | None = None) -> dict[str, Any]:
        run_or_404(run_id)
        reason = body.reason if body else "stopped by operator"
        stopped = await runs.stop(run_id, actor="operator", reason=reason)
        return {"stopped": stopped, "run": _run_json(runs.get(run_id))}

    # --- policy ----------------------------------------------------------------

    @app.get("/policy", dependencies=auth)
    def get_policy() -> dict[str, Any]:
        return {"version": engine.version, "policy": engine.policy.model_dump(mode="json", exclude_none=True)}

    def _announce_policy(version: int) -> None:
        bus.publish(Event(run_id="system", kind="policy_reloaded", reason=f"policy v{version} active"))

    @app.post("/policy/reload", dependencies=auth)
    def reload_policy() -> dict[str, Any]:
        try:
            version = engine.reload()
        except PolicyError as e:
            raise HTTPException(400, f"policy rejected, previous policy kept: {e}") from None
        _announce_policy(version)
        return {"version": version}

    @app.put("/policy", dependencies=auth)
    def replace_policy(body: dict[str, Any]) -> dict[str, Any]:
        try:
            policy: Policy = parse_policy(body)
        except PolicyError as e:
            raise HTTPException(400, f"policy rejected, previous policy kept: {e}") from None
        version = engine.replace(policy)
        _announce_policy(version)
        return {"version": version}

    # --- live stream -------------------------------------------------------------

    @app.websocket("/ws")
    async def stream(ws: WebSocket, run_id: str | None = None, token: str | None = None) -> None:
        # Browsers can't set headers on WebSockets, so accept ?token= as well.
        header = ws.headers.get("authorization", "")
        candidate = token or (header[7:] if header.lower().startswith("bearer ") else None)
        if not token_ok(candidate):
            await ws.close(code=WS_UNAUTHORIZED)
            return
        await ws.accept()
        sub = bus.subscribe(run_id=run_id)

        async def pump() -> None:
            while True:
                event = await sub.get()
                if event is None:
                    return
                await ws.send_text(json.dumps(event.model_dump(mode="json")))

        async def watch_disconnect() -> None:
            # Without this, a closed client is only noticed on the next event,
            # leaking the subscription and blocking server shutdown.
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    return

        tasks = [asyncio.create_task(pump()), asyncio.create_task(watch_disconnect())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            sub.close()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    return app
