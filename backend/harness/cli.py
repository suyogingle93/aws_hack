"""`harness` command line: serve, run, tail, stop, runs."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import typer

from .launcher import (
    API_ENV,
    DEFAULT_API,
    HOME_ENV,
    HarnessClient,
    LauncherError,
    resolve_operator_token,
    run_agent,
)

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Control layer for CLI and desktop AI agents.")

ApiOpt = typer.Option(DEFAULT_API, "--api", envvar=API_ENV, help="Harness control API URL.")


def _client(api: str) -> HarnessClient:
    home = Path(os.environ[HOME_ENV]) if os.environ.get(HOME_ENV) else None
    return HarnessClient(api, resolve_operator_token(os.environ, home))


def _fail(e: LauncherError) -> typer.Exit:
    typer.secho(f"error: {e}", fg=typer.colors.RED, err=True)
    return typer.Exit(e.exit_code)


# --- event formatting ---------------------------------------------------------------

def format_event(e: dict, color: bool = True) -> str | None:
    """One readable line per event, or None for events not worth showing."""

    def paint(text: str, fg: str, bold: bool = False) -> str:
        return typer.style(text, fg=fg, bold=bold) if color else text

    try:
        stamp = datetime.fromisoformat(e["ts"].replace("Z", "+00:00")).astimezone().strftime("%H:%M:%S")
    except (KeyError, ValueError):
        stamp = "--:--:--"
    run = f"[{e.get('run_id', '?')}]"
    kind = e.get("kind")

    if kind == "request":
        decision = e.get("decision")
        if decision == "allowed":
            badge = paint("ALLOW  ", "green", True)
        elif decision == "blocked":
            badge = paint("BLOCK  ", "red", True)
        else:
            badge = paint("...    ", "yellow")
        target = f"{e.get('host') or ''}{e.get('path') or ''}"
        if len(target) > 90:
            target = target[:87] + "..."
        parts = [stamp, run, badge, f"{e.get('method') or '':<6}", target]
        if e.get("tool"):
            parts.append(f"tool={e['tool']}")
        if e.get("status_code"):
            parts.append(str(e["status_code"]))
        if decision == "blocked":
            why = e.get("rule_id") or e.get("reason") or ""
            parts.append(paint(f"({why})", "red"))
        elif e.get("rule_id"):
            parts.append(paint(f"({e['rule_id']})", "bright_black"))
        return " ".join(parts)

    labels = {
        "run_started": ("RUN STARTED ", "cyan"),
        "run_stopped": ("RUN STOPPED ", "red"),
        "run_finished": ("RUN FINISHED", "cyan"),
        "policy_reloaded": ("POLICY      ", "magenta"),
    }
    if kind in labels:
        text, fg = labels[kind]
        return f"{stamp} {run} {paint(text, fg, True)} {e.get('reason') or ''}".rstrip()
    return None


# --- commands -------------------------------------------------------------------------

@app.command()
def serve(
    policy: Path = typer.Option(None, help="Policy YAML file (default: policies/demo.yaml)."),
    proxy_port: int = typer.Option(8080, help="Port for the gateway proxy."),
    api_port: int = typer.Option(8000, help="Port for the control API."),
    home: Path = typer.Option(None, help="State directory (default: ~/.agent-harness)."),
) -> None:
    """Start the gateway and control API."""
    from .main import DEFAULT_HOME, DEFAULT_POLICY, Settings, serve as serve_backend

    settings = Settings(
        policy_path=policy or DEFAULT_POLICY,
        home=home or DEFAULT_HOME,
        proxy_port=proxy_port,
        api_port=api_port,
        operator_token=os.environ.get("HARNESS_OPERATOR_TOKEN"),
    )
    try:
        asyncio.run(serve_backend(settings))
    except Exception as e:  # startup errors: bad policy, port in use...
        typer.secho(f"error: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from None


@app.command()
def run(
    command: list[str] = typer.Argument(..., help="The agent command, after `--`."),
    api: str = ApiOpt,
    cwd: Optional[Path] = typer.Option(None, help="Working directory for the agent."),
) -> None:
    """Run an agent under the harness:  harness run -- <command> [args...]"""
    try:
        client = _client(api)
        try:
            code = run_agent(client, command, cwd=str(cwd) if cwd else None)
        finally:
            client.close()
    except LauncherError as e:
        raise _fail(e) from None
    raise typer.Exit(code)


@app.command()
def tail(
    run_id: Optional[str] = typer.Argument(None, help="Only this run (default: all runs)."),
    api: str = ApiOpt,
    color: bool = typer.Option(True, "--color/--no-color"),
) -> None:
    """Stream live traffic and decisions. Exits when the given run ends."""
    from websockets.exceptions import ConnectionClosed, InvalidStatus
    from websockets.sync.client import connect

    try:
        token = resolve_operator_token(os.environ, Path(os.environ[HOME_ENV]) if os.environ.get(HOME_ENV) else None)
    except LauncherError as e:
        raise _fail(e) from None

    ws_url = api.rstrip("/").replace("http://", "ws://", 1).replace("https://", "wss://", 1) + "/ws"
    if run_id:
        ws_url += f"?run_id={run_id}"
    try:
        with connect(ws_url, additional_headers={"Authorization": f"Bearer {token}"}) as ws:
            typer.secho(f"streaming {'run ' + run_id if run_id else 'all runs'} (Ctrl+C to quit)", err=True)
            for message in ws:
                event = json.loads(message)
                line = format_event(event, color)
                if line:
                    typer.echo(line)
                if run_id and event.get("kind") in ("run_stopped", "run_finished"):
                    return
    except KeyboardInterrupt:
        return
    except InvalidStatus:
        raise _fail(LauncherError("the harness backend rejected the operator token")) from None
    except ConnectionClosed:
        typer.secho("connection closed by the backend", err=True)
    except OSError:
        raise _fail(LauncherError(f"cannot reach the harness backend at {api}. Start it with: harness serve")) from None


@app.command()
def stop(
    run_id: str = typer.Argument(..., help="Run to stop."),
    reason: str = typer.Option("stopped by operator", help="Recorded in the audit log."),
    api: str = ApiOpt,
) -> None:
    """Kill switch: terminate the agent and revoke its access."""
    try:
        client = _client(api)
        try:
            result = client.stop(run_id, reason)
        finally:
            client.close()
    except LauncherError as e:
        raise _fail(e) from None
    if result["stopped"]:
        typer.secho(f"stopped run {run_id}", fg=typer.colors.GREEN)
    else:
        typer.echo(f"run {run_id} was already {result['run']['status']}")


@app.command()
def runs(api: str = ApiOpt) -> None:
    """List runs with their status and allow/block counts."""
    try:
        client = _client(api)
        try:
            items = client.runs()
        finally:
            client.close()
    except LauncherError as e:
        raise _fail(e) from None
    if not items:
        typer.echo("no runs yet")
        return
    typer.echo(f"{'RUN':<10}{'STATUS':<10}{'ALLOWED':>8}{'BLOCKED':>9}  COMMAND")
    for r in items:
        typer.echo(f"{r['id']:<10}{r['status']:<10}{r['allowed']:>8}{r['blocked']:>9}  {' '.join(r['command'])}")


if __name__ == "__main__":
    app()
