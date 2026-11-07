"""Checkpoint (task 10): launch -> live traffic -> block -> stop, scripted and verified.

    ./venv/bin/python backend/demo/checkpoint.py            # auto-detects AWS credentials
    ./venv/bin/python backend/demo/checkpoint.py --skip-bedrock
    ./venv/bin/python backend/demo/checkpoint.py --keep     # keep logs afterwards

Runs a private backend on free ports with a throwaway state directory, so it never
touches a `harness serve` you already have running. Exits 0 only if every required
check passes.
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

BACKEND = Path(__file__).resolve().parents[1]
DEMO_AGENT = BACKEND / "demo" / "demo_agent.py"
POLICY = BACKEND / "policies" / "demo.yaml"
PY = sys.executable

GREEN, RED, YELLOW, BOLD, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[1m", "\033[2m", "\033[0m"
if not sys.stdout.isatty():
    GREEN = RED = YELLOW = BOLD = DIM = RESET = ""


@dataclass
class Check:
    name: str
    status: str  # PASS / WARN / FAIL / SKIP
    detail: str = ""


def say(msg: str) -> None:
    print(f"{BOLD}==>{RESET} {msg}", flush=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def have_aws_credentials() -> bool:
    try:
        import boto3

        return boto3.Session().get_credentials() is not None
    except Exception:  # noqa: BLE001
        return False


def wait_until(predicate, timeout: float, interval: float = 0.2) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def stop_process(proc: subprocess.Popen | None, sig: int = signal.SIGINT, timeout: float = 15) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.send_signal(sig)
    try:
        proc.wait(timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skip-bedrock", action="store_true", help="skip the Bedrock steps")
    ap.add_argument("--keep", action="store_true", help="keep logs and state afterwards")
    ap.add_argument("--pause", type=float, default=0.5, help="seconds between demo steps (default 0.5)")
    args = ap.parse_args()

    use_bedrock = not args.skip_bedrock and have_aws_credentials()
    if not args.skip_bedrock and not use_bedrock:
        print(f"{YELLOW}No AWS credentials found; skipping Bedrock steps. "
              f"Set them up (Bedrock guide, 1-setup) to include Bedrock.{RESET}")

    work = Path(tempfile.mkdtemp(prefix="harness-checkpoint-"))
    home = work / "home"
    proxy_port, api_port = free_port(), free_port()
    api = f"http://127.0.0.1:{api_port}"
    env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower() and not k.startswith("HARNESS_")}
    env.update(HARNESS_HOME=str(home), HARNESS_API=api, PYTHONPATH=str(BACKEND))

    serve = tail = None
    checks: list[Check] = []
    failed = False
    try:
        # 1. Backend -----------------------------------------------------------------------
        say(f"Starting the harness backend (proxy :{proxy_port}, API :{api_port})")
        serve_log = open(work / "serve.log", "w")
        serve = subprocess.Popen(
            [PY, "-m", "harness", "serve", "--policy", str(POLICY), "--home", str(home),
             "--proxy-port", str(proxy_port), "--api-port", str(api_port)],
            cwd=BACKEND, env=env, stdout=serve_log, stderr=subprocess.STDOUT, start_new_session=True,
        )

        def healthy() -> bool:
            if serve.poll() is not None:
                return True  # exited; checked below
            try:
                return httpx.get(f"{api}/health", timeout=1, trust_env=False).status_code == 200 \
                    and (home / "operator_token").exists()
            except httpx.HTTPError:
                return False

        if not wait_until(healthy, 30) or serve.poll() is not None:
            print((work / "serve.log").read_text())
            raise SystemExit(f"{RED}backend failed to start (log above){RESET}")
        token = (home / "operator_token").read_text().strip()
        client = httpx.Client(base_url=api, headers={"Authorization": f"Bearer {token}"}, trust_env=False)

        # 2. Live view -----------------------------------------------------------------------
        say("Starting `harness tail` (the live traffic view)")
        tail_out, tail_err = open(work / "tail.log", "w"), open(work / "tail.err", "w")
        tail = subprocess.Popen([PY, "-m", "harness", "tail", "--no-color"], cwd=BACKEND, env=env,
                                stdout=tail_out, stderr=tail_err)
        if not wait_until(lambda: "streaming" in (work / "tail.err").read_text(), 15):
            raise SystemExit(f"{RED}tail did not connect: {(work / 'tail.err').read_text()}{RESET}")

        # 3. The demo run ---------------------------------------------------------------------
        say("Running the demo agent under the harness\n")
        agent_env = {**env, "DEMO_PAUSE": str(args.pause), "DEMO_SKIP_BEDROCK": "0" if use_bedrock else "1"}
        agent = subprocess.Popen([PY, "-m", "harness", "run", "--", PY, str(DEMO_AGENT)], cwd=BACKEND,
                                 env=agent_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        agent_lines: list[str] = []
        assert agent.stdout is not None
        for line in agent.stdout:
            agent_lines.append(line)
            print(f"  {DIM}agent |{RESET} {line}", end="", flush=True)
        try:
            agent_code = agent.wait(180)
        except subprocess.TimeoutExpired:
            agent.kill()
            agent_code = agent.wait()
        print()

        # 4. Collect what the harness recorded -----------------------------------------------
        runs = client.get("/runs").json()
        run = runs[0] if runs else None
        events: list[dict] = []
        if run:
            def stop_persisted() -> bool:
                nonlocal events
                events = client.get(f"/runs/{run['id']}/events").json()
                return any(e["kind"] in ("run_stopped", "run_finished") for e in events)
            wait_until(stop_persisted, 10)
        wait_until(lambda: "RUN STOPPED" in (work / "tail.log").read_text(), 5)
        tail_text = (work / "tail.log").read_text()
        requests = [e for e in events if e["kind"] == "request"]

        def find(host_part: str, path_part: str = "") -> list[dict]:
            return [e for e in requests if host_part in (e.get("host") or "") and path_part in (e.get("path") or "")]

        # 5. Checks ---------------------------------------------------------------------------
        checks.append(Check("Agent launched under the harness",
                            "PASS" if run and run.get("pid") else "FAIL",
                            f"run {run['id']}, pid {run.get('pid')}" if run else "no run registered"))

        checks.append(Check("Traffic captured and shown live",
                            "PASS" if requests and "ALLOW" in tail_text else "FAIL",
                            f"{len(requests)} requests recorded, {tail_text.count(chr(10))} lines in tail"))

        github = find("api.github.com")
        gh_allowed = [e for e in github if e.get("decision") == "allowed"]
        if not gh_allowed:
            checks.append(Check("GitHub read allowed", "FAIL", "no allowed GitHub request recorded"))
        elif gh_allowed[0].get("status_code") == 502:
            checks.append(Check("GitHub read allowed", "WARN",
                                "allowed by policy, but upstream unreachable (no internet?)"))
        else:
            checks.append(Check("GitHub read allowed", "PASS",
                                f"rule {gh_allowed[0].get('rule_id')}, status {gh_allowed[0].get('status_code')}"))

        if use_bedrock:
            approved = [e for e in find("bedrock-runtime", "llama4-scout") if e.get("decision") == "allowed"]
            if not approved:
                checks.append(Check("Approved Bedrock model allowed", "FAIL", "no allowed request to the approved model"))
            elif approved[0].get("status_code") not in (200, None):
                checks.append(Check("Approved Bedrock model allowed", "WARN",
                                    f"allowed by the harness, but Bedrock returned {approved[0].get('status_code')} "
                                    "(check model access / credentials)"))
            else:
                checks.append(Check("Approved Bedrock model allowed", "PASS", "allow-bedrock-approved-model"))
            other = [e for e in find("bedrock-runtime", "titan") if e.get("decision") == "blocked"]
            checks.append(Check("Unapproved Bedrock model blocked", "PASS" if other else "FAIL",
                                other[0].get("reason", "") if other else "not blocked"))
        else:
            checks.append(Check("Bedrock (approved allowed / other blocked)", "SKIP", "no AWS credentials"))

        paste = [e for e in find("pastebin.com") if e.get("decision") == "blocked"]
        checks.append(Check("Disallowed action blocked (exfiltration to pastebin)",
                            "PASS" if paste and paste[0].get("rule_id") == "block-paste-sites" else "FAIL",
                            f"rule {paste[0].get('rule_id')}, HTTP {paste[0].get('status_code')}" if paste else "not blocked"))

        stopped_ev = [e for e in events if e["kind"] == "run_stopped"]
        stopped_ok = bool(run) and run["status"] == "stopped" and stopped_ev and "policy" in stopped_ev[0].get("reason", "")
        checks.append(Check("Run stopped by policy (kill switch)", "PASS" if stopped_ok else "FAIL",
                            stopped_ev[0]["reason"] if stopped_ev else f"run status: {run and run['status']}"))

        checks.append(Check("Agent process terminated", "PASS" if agent_code == 143 else "FAIL",
                            f"launcher exit code {agent_code} (143 = SIGTERM from the harness)"))

        after = [e for e in gh_allowed[1:]]  # step 5 must never get through
        checks.append(Check("Nothing allowed after the stop", "PASS" if not after else "FAIL",
                            "no further requests reached the internet" if not after else f"{len(after)} got through"))

        checks.append(Check("Live view shows the block and the stop",
                            "PASS" if "BLOCK" in tail_text and "RUN STOPPED" in tail_text else "FAIL"))

        client.close()
    finally:
        stop_process(tail, signal.SIGTERM)
        stop_process(serve, signal.SIGINT)  # graceful: stops any agents, removes the token file

    # 6. Report -----------------------------------------------------------------------------------
    colors = {"PASS": GREEN, "WARN": YELLOW, "FAIL": RED, "SKIP": DIM}
    print(f"{BOLD}Checkpoint results{RESET}")
    for c in checks:
        print(f"  {colors[c.status]}{c.status:<4}{RESET}  {c.name}" + (f"  {DIM}- {c.detail}{RESET}" if c.detail else ""))
    failed = any(c.status == "FAIL" for c in checks) or not checks

    tail_copy = (work / "tail.log").read_text() if (work / "tail.log").exists() else ""
    if tail_copy:
        print(f"\n{BOLD}What `harness tail` showed{RESET}")
        for line in tail_copy.splitlines():
            print(f"  {line}")

    if args.keep or failed:
        print(f"\nLogs kept in {work}  (serve.log contains this session's operator token; the session is over)")
    else:
        shutil.rmtree(work, ignore_errors=True)

    print(f"\n{RED + 'CHECKPOINT FAILED' if failed else GREEN + 'CHECKPOINT PASSED'}{RESET}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
