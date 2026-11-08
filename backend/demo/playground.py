"""Interactive playground: type requests and see how the harness decides.

Run it UNDER the harness (otherwise nothing is checked):

    ./venv/bin/harness run -- ./venv/bin/python backend/demo/playground.py

Or pipe commands in:

    printf 'get https://example.com\\ntool delete_repo\\n' | ./venv/bin/harness run -- ./venv/bin/python backend/demo/playground.py
"""

from __future__ import annotations

import json
import os
import shlex
import sys

import httpx

APPROVED_MODEL = os.getenv("BEDROCK_MODEL_ID", "us.meta.llama4-scout-17b-instruct-v1:0")
DEFAULT_MCP_URL = "https://mcp.example.com/mcp"

HELP = f"""\
Commands (each request goes through the harness gateway):
  get <url>                         HTTP GET
  post <url> [text]                 HTTP POST with a text body
  tool <name> [url] [json-args]     MCP JSON-RPC tools/call (default url {DEFAULT_MCP_URL})
  ask <prompt>                      Bedrock, approved model ({APPROVED_MODEL})
  ask-model <model-id> <prompt>     Bedrock, any model, e.g. amazon.titan-text-lite-v1
  help | quit
Tip: edit backend/policies/demo.yaml while this runs; the harness applies it on save."""

GREEN, RED, DIM, RESET = ("\033[32m", "\033[31m", "\033[2m", "\033[0m") if sys.stdout.isatty() else ("",) * 4


def verdict_http(r: httpx.Response) -> None:
    if r.headers.get("x-harness-blocked"):
        try:
            body = r.json()
        except ValueError:
            body = {}
        rule = f"  [rule: {body['rule']}]" if body.get("rule") else ""
        print(f"{RED}BLOCKED by the harness{RESET} (HTTP {r.status_code}): {body.get('reason', 'blocked')}{rule}")
    else:
        snippet = r.text.strip().replace("\n", " ")[:120]
        print(f"{GREEN}ALLOWED{RESET}, upstream answered HTTP {r.status_code}: {DIM}{snippet}{RESET}")


def http(method: str, url: str, **kw) -> None:
    try:
        r = httpx.request(method, url, timeout=20, **kw)
    except httpx.HTTPError as e:
        print(f"{RED}request failed{RESET}: {type(e).__name__}: {e}")
        return
    verdict_http(r)


def tool(name: str, url: str = DEFAULT_MCP_URL, args: str = "{}") -> None:
    try:
        arguments = json.loads(args)
    except ValueError:
        print("json-args must be valid JSON, e.g. '{\"repo\": \"demo\"}'")
        return
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    print(f"{DIM}  -> POST {url}  tools/call {name} {json.dumps(arguments)}{RESET}")
    http("POST", url, json=body)


def ask(model_id: str, prompt: str) -> None:
    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError, NoCredentialsError

    client = boto3.client(
        "bedrock-runtime",
        region_name=os.getenv("AWS_REGION", "us-east-1"),
        config=Config(retries={"max_attempts": 1}),
    )
    try:
        resp = client.converse_stream(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": 300},
        )
        print(f"{GREEN}ALLOWED{RESET}, {model_id} says: ", end="", flush=True)
        for event in resp["stream"]:
            text = event.get("contentBlockDelta", {}).get("delta", {}).get("text")
            if text:
                print(text, end="", flush=True)
        print()
    except NoCredentialsError:
        print("no AWS credentials in this terminal; paste the Workshop Studio export block first")
    except ClientError as e:
        headers = e.response.get("ResponseMetadata", {}).get("HTTPHeaders", {})
        if headers.get("x-harness-blocked"):
            print(f"{RED}BLOCKED by the harness{RESET}: {model_id} is not an approved model (AWS was never called)")
        else:
            print(f"AWS refused it: {e}")


def handle(line: str) -> bool:
    try:
        parts = shlex.split(line)
    except ValueError as e:
        print(f"could not parse: {e}")
        return True
    if not parts:
        return True
    cmd, args = parts[0].lower(), parts[1:]
    if cmd in ("quit", "exit"):
        return False
    if cmd == "help":
        print(HELP)
    elif cmd == "get" and len(args) == 1:
        http("GET", args[0])
    elif cmd == "post" and len(args) in (1, 2):
        http("POST", args[0], content=(args[1] if len(args) == 2 else "").encode())
    elif cmd == "tool" and 1 <= len(args) <= 3:
        tool(*args)
    elif cmd == "ask" and args:
        ask(APPROVED_MODEL, " ".join(args))
    elif cmd == "ask-model" and len(args) >= 2:
        ask(args[0], " ".join(args[1:]))
    else:
        print("unknown command or wrong arguments; type `help`")
    return True


def main() -> int:
    if not os.getenv("HARNESS_RUN_ID"):
        print(f"{RED}warning:{RESET} not running under the harness, so nothing is being checked.\n"
              "Start it with: ./venv/bin/harness run -- ./venv/bin/python backend/demo/playground.py\n")
    interactive = sys.stdin.isatty()
    if interactive:
        print(f"Agent Harness playground (run {os.getenv('HARNESS_RUN_ID', '-')}). Type `help`.")
    while True:
        try:
            line = input("harness> " if interactive else "")
        except EOFError:
            return 0
        if not interactive:
            print(f"\nharness> {line}")
        if not handle(line):
            return 0


if __name__ == "__main__":
    sys.exit(main())
