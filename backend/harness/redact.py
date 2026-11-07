"""Secret redaction. Applied before anything is published or persisted."""

from __future__ import annotations

import re
from typing import Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REDACTED = "[REDACTED]"

SENSITIVE_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "api-key",
        "x-auth-token",
        "x-amz-security-token",
    }
)

SENSITIVE_QUERY_KEYS = frozenset(
    {
        "token",
        "access_token",
        "api_key",
        "apikey",
        "key",
        "password",
        "secret",
        "signature",
        "x-amz-signature",
        "x-amz-security-token",
        "x-amz-credential",
    }
)

# Secrets that can show up inside free text (paths, bodies, error messages).
_TOKEN_PATTERNS = [
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),  # AWS access key IDs
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),  # GitHub tokens
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),  # common API key prefix
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)\bSignature=[0-9a-f]{16,}"),
]


def redact_text(text: str) -> str:
    for pattern in _TOKEN_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def _redact_header_value(value: str) -> str:
    """Keep the auth scheme (useful for debugging) but drop the credential."""
    parts = value.split(None, 1)
    if len(parts) == 2 and re.fullmatch(r"[A-Za-z0-9-]+", parts[0]):
        return f"{parts[0]} {REDACTED}"
    return REDACTED


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, value in headers.items():
        if name.lower() in SENSITIVE_HEADERS:
            if name.lower() in {"authorization", "proxy-authorization"}:
                out[name] = _redact_header_value(str(value))
            else:
                out[name] = REDACTED
        else:
            out[name] = redact_text(str(value))
    return out


def redact_path(path: str) -> str:
    """Redact sensitive query parameters and token-like strings in a request path."""
    parts = urlsplit(path)
    if parts.query:
        pairs = [
            (k, REDACTED if k.lower() in SENSITIVE_QUERY_KEYS else v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
        ]
        parts = parts._replace(query=urlencode(pairs, safe="[]"))
    return redact_text(urlunsplit(parts))
