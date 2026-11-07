"""Extract MCP tool calls from JSON-RPC request bodies."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

MAX_BODY_BYTES = 1_000_000  # skip parsing anything larger


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


def extract_tool_calls(body: bytes | str | None) -> list[ToolCall]:
    """Return every `tools/call` found in a JSON-RPC body (single or batch).

    Anything that is not a valid JSON-RPC tool call yields an empty list, so
    ordinary traffic falls through to host/path rules.
    """
    if not body:
        return []
    if len(body) > MAX_BODY_BYTES:
        return []
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return []

    messages = data if isinstance(data, list) else [data]
    calls: list[ToolCall] = []
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("method") != "tools/call":
            continue
        params = msg.get("params")
        if not isinstance(params, dict):
            continue
        name = params.get("name")
        if not isinstance(name, str) or not name:
            continue
        args = params.get("arguments")
        calls.append(ToolCall(name=name, arguments=args if isinstance(args, dict) else {}))
    return calls


def tool_names(body: bytes | str | None) -> tuple[str, ...]:
    return tuple(c.name for c in extract_tool_calls(body))
