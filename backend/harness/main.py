"""Backend entrypoint: mitmproxy gateway + control API in one process and event loop.

    python -m harness.main --policy policies/demo.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import secrets
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import uvicorn
from mitmproxy import options
from mitmproxy.tools.dump import DumpMaster

from .api import ApiConfig, create_app
from .events import EventBus
from .gateway import HarnessGateway
from .policy import PolicyEngine
from .runs import RunManager
from .store import SqliteStore, StoreWriter
from .watcher import watch_policy

log = logging.getLogger("harness")

DEFAULT_HOME = Path(os.environ.get("HARNESS_HOME", Path.home() / ".agent-harness"))
DEFAULT_POLICY = Path(__file__).resolve().parents[1] / "policies" / "demo.yaml"
TOKEN_FILENAME = "operator_token"


@dataclass
class Settings:
    policy_path: Path = DEFAULT_POLICY
    home: Path = DEFAULT_HOME
    proxy_host: str = "127.0.0.1"  # localhost by default (Req 9.2)
    proxy_port: int = 8080
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    db_path: Path | None = None  # default: <home>/harness.sqlite3
    operator_token: str | None = None  # default: generated
    watch_interval: float = 1.0
    stop_runs_on_shutdown: bool = True
    cors_origins: tuple[str, ...] = field(
        default_factory=lambda: ("http://localhost:5173", "http://127.0.0.1:5173")
    )


class _Server(uvicorn.Server):
    """uvicorn without its own signal handlers; the backend owns shutdown."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


class Backend:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.bus = EventBus()
        self.operator_token = settings.operator_token or secrets.token_urlsafe(32)
        self.proxy_port = 0
        self.api_port = 0
        self.ca_path: Path | None = None
        self.token_file: Path | None = None
        self._tasks: list[asyncio.Task] = []
        self._started = False

    async def start(self) -> None:
        s = self.settings
        s.home.mkdir(parents=True, exist_ok=True, mode=0o700)

        self.engine = PolicyEngine.from_file(s.policy_path)  # fail fast on a bad policy
        self.store = SqliteStore(str(s.db_path or s.home / "harness.sqlite3"))
        self.runs = RunManager(self.bus, self.store, on_run_end=self.engine.forget_run)
        self.gateway = HarnessGateway(self.engine, self.runs, self.bus)
        self.writer = StoreWriter(self.bus, self.store)
        self.writer.start()

        # --- proxy ---
        confdir = s.home / "mitmproxy"
        opts = options.Options(listen_host=s.proxy_host, listen_port=s.proxy_port, confdir=str(confdir))
        self._master = DumpMaster(opts, with_termlog=False, with_dumper=False)
        self._master.addons.add(self.gateway)
        self._master_task = asyncio.create_task(self._run_master(), name="mitmproxy")
        proxyserver = self._master.addons.get("proxyserver")
        ca = confdir / "mitmproxy-ca-cert.pem"
        for _ in range(200):
            if self._master_task.done():
                error = self._master_task.exception() or RuntimeError("proxy exited during startup")
                await self._abort()
                raise error
            if proxyserver.listen_addrs() and ca.exists():
                break
            await asyncio.sleep(0.05)
        else:
            await self._abort()
            raise RuntimeError("proxy did not start in time")
        self.proxy_port = proxyserver.listen_addrs()[0][1]
        self.ca_path = ca

        # --- control API ---
        app = create_app(
            ApiConfig(
                operator_token=self.operator_token,
                proxy_host=s.proxy_host,
                proxy_port=self.proxy_port,
                ca_path=str(ca),
                cors_origins=s.cors_origins,
            ),
            self.runs,
            self.engine,
            self.bus,
            self.store,
        )
        self._api = _Server(uvicorn.Config(app, host=s.api_host, port=s.api_port, log_level="warning"))
        self._api_task = asyncio.create_task(self._api.serve(), name="control-api")
        while not self._api.started:
            if self._api_task.done():
                await self._abort()
                self._api_task.result()
                raise RuntimeError("control API failed to start")
            await asyncio.sleep(0.02)
        self.api_port = self._api.servers[0].sockets[0].getsockname()[1]

        # The agent must never reach the harness's own ports through the proxy.
        self.gateway.protected_ports.update({self.proxy_port, self.api_port})

        # Operator token for the CLI: owner-only file. (Documented limit: a managed agent
        # running as the same OS user could read it.)
        self.token_file = s.home / TOKEN_FILENAME
        fd = os.open(self.token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(self.operator_token)
        os.chmod(self.token_file, 0o600)

        self._tasks.append(asyncio.create_task(watch_policy(self.engine, self.bus, s.watch_interval), name="policy-watch"))
        self._started = True

    async def _run_master(self) -> None:
        # mitmproxy calls sys.exit(1) on startup errors; a SystemExit inside a task would
        # tear down the whole event loop, so turn it into an ordinary exception.
        try:
            await self._master.run()
        except SystemExit as e:
            raise RuntimeError(
                f"proxy failed to start on {self.settings.proxy_host}:{self.settings.proxy_port} "
                "(is the port already in use?)"
            ) from e

    async def _abort(self) -> None:
        self._master.shutdown()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._master_task, 5)
        await self.writer.stop()
        self.store.close()

    async def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

        # Fail closed: without the gateway, managed agents must not keep running unsupervised.
        if self.settings.stop_runs_on_shutdown:
            for view in self.runs.list_runs():
                if view.record.status == "running":
                    await self.runs.stop(view.record.id, actor="system", reason="harness shutting down")

        self._api.should_exit = True
        await self._api_task
        self._master.shutdown()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._master_task, 10)
        await self.writer.stop()
        self.store.close()
        if self.token_file:
            with contextlib.suppress(OSError):
                self.token_file.unlink()


async def serve(settings: Settings) -> None:
    backend = Backend(settings)
    await backend.start()
    print(
        "\nAgent Harness is running\n"
        f"  proxy:      http://{settings.proxy_host}:{backend.proxy_port}\n"
        f"  control API: http://{settings.api_host}:{backend.api_port}\n"
        f"  policy:     {settings.policy_path}\n"
        f"  CA cert:    {backend.ca_path}\n"
        f"  operator token (also in {backend.token_file}):\n    {backend.operator_token}\n",
        flush=True,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    print("Shutting down (stopping any managed agents)...", flush=True)
    await backend.stop()


def parse_args(argv: list[str] | None = None) -> Settings:
    p = argparse.ArgumentParser(prog="harness-server", description="Run the Agent Harness backend")
    p.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    p.add_argument("--home", type=Path, default=DEFAULT_HOME)
    p.add_argument("--proxy-host", default="127.0.0.1")
    p.add_argument("--proxy-port", type=int, default=8080)
    p.add_argument("--api-host", default="127.0.0.1")
    p.add_argument("--api-port", type=int, default=8000)
    a = p.parse_args(argv)
    return Settings(
        policy_path=a.policy, home=a.home, proxy_host=a.proxy_host, proxy_port=a.proxy_port,
        api_host=a.api_host, api_port=a.api_port,
        operator_token=os.environ.get("HARNESS_OPERATOR_TOKEN"),
    )


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    asyncio.run(serve(parse_args(argv)))


if __name__ == "__main__":
    main()
