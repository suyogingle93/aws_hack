"""Policy model and evaluation.

A policy is a list of allow/deny rules plus a default action. Deny always beats
allow, and a per-run rate limit is checked first. The engine swaps policies
atomically so a reload never leaves requests evaluated against a half-loaded
policy, and an invalid file never replaces a working one.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Callable, Literal
from urllib.parse import unquote, urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

Action = Literal["allow", "deny"]


class PolicyError(ValueError):
    """Raised when a policy file or document is invalid."""


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    action: Action
    host: str | None = None
    path: str | None = None
    methods: list[str] | None = None
    mcp_tool: str | None = None
    terminate_on_violation: bool = False
    description: str | None = None

    @model_validator(mode="after")
    def _check(self) -> "Rule":
        if not any([self.host, self.path, self.methods, self.mcp_tool]):
            raise ValueError(f"rule '{self.id}' must set at least one of host, path, methods, mcp_tool")
        if self.terminate_on_violation and self.action != "deny":
            raise ValueError(f"rule '{self.id}': terminate_on_violation only applies to deny rules")
        if self.methods:
            self.methods = [m.upper() for m in self.methods]
        return self


class RateLimit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requests_per_minute: int = Field(gt=0)


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default: Action = "deny"
    rate_limit: RateLimit | None = None
    rules: list[Rule] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_ids(self) -> "Policy":
        seen: set[str] = set()
        for rule in self.rules:
            if rule.id in seen:
                raise ValueError(f"duplicate rule id '{rule.id}'")
            seen.add(rule.id)
        return self


@dataclass(frozen=True)
class RequestContext:
    """What the policy engine needs to know about one request."""

    run_id: str
    method: str
    host: str
    path: str  # may include a query string and percent-encoding
    tools: tuple[str, ...] = ()  # MCP tool names found in the body, if any


@dataclass(frozen=True)
class Decision:
    action: Action
    rule_id: str | None
    reason: str
    terminate: bool = False

    @property
    def allowed(self) -> bool:
        return self.action == "allow"


def _glob(pattern: str, value: str) -> bool:
    return fnmatchcase(value.lower(), pattern.lower())


def _normalize_path(raw: str) -> str:
    """Drop the query string and percent-decode (boto3 encodes ':' in model IDs)."""
    return unquote(urlsplit(raw).path) or "/"


def rule_matches(rule: Rule, ctx: RequestContext) -> bool:
    """A rule matches only if every field it specifies matches."""
    if rule.host and not _glob(rule.host, ctx.host):
        return False
    if rule.path and not _glob(rule.path, _normalize_path(ctx.path)):
        return False
    if rule.methods and ctx.method.upper() not in rule.methods:
        return False
    if rule.mcp_tool and not any(_glob(rule.mcp_tool, t) for t in ctx.tools):
        return False
    return True


def evaluate_policy(policy: Policy, ctx: RequestContext) -> Decision:
    """Pure rule evaluation (no rate limiting). Deny beats allow."""
    matched = [r for r in policy.rules if rule_matches(r, ctx)]

    for rule in matched:
        if rule.action == "deny":
            return Decision("deny", rule.id, f"denied by rule '{rule.id}'", rule.terminate_on_violation)
    for rule in matched:
        if rule.action == "allow":
            return Decision("allow", rule.id, f"allowed by rule '{rule.id}'")

    return Decision(policy.default, None, f"no rule matched; default {policy.default}")


class _RateLimiter:
    """Sliding one-minute window per run."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def exceeded(self, run_id: str, limit: int) -> bool:
        now = self._clock()
        hits = self._hits[run_id]
        while hits and now - hits[0] >= 60.0:
            hits.popleft()
        if len(hits) >= limit:
            return True
        hits.append(now)
        return False

    def forget(self, run_id: str) -> None:
        self._hits.pop(run_id, None)


def parse_policy(data: object) -> Policy:
    if not isinstance(data, dict):
        raise PolicyError("policy must be a mapping with 'default' and 'rules'")
    try:
        return Policy.model_validate(data)
    except ValidationError as e:
        raise PolicyError(str(e)) from e


def load_policy_file(path: str | Path) -> Policy:
    try:
        text = Path(path).read_text()
    except OSError as e:
        raise PolicyError(f"cannot read policy file {path}: {e}") from e
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise PolicyError(f"invalid YAML in {path}: {e}") from e
    return parse_policy(data if data is not None else {})


@dataclass
class PolicyEngine:
    """Thread-safe holder for the active policy."""

    policy: Policy
    source: Path | None = None
    clock: Callable[[], float] = time.monotonic
    version: int = 1
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _limiter: _RateLimiter = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._limiter = _RateLimiter(self.clock)

    @classmethod
    def from_file(cls, path: str | Path, **kwargs) -> "PolicyEngine":
        return cls(policy=load_policy_file(path), source=Path(path), **kwargs)

    def evaluate(self, ctx: RequestContext) -> Decision:
        with self._lock:
            policy = self.policy
            limited = bool(policy.rate_limit) and self._limiter.exceeded(
                ctx.run_id, policy.rate_limit.requests_per_minute
            )
        if limited:
            return Decision("deny", None, "rate limit exceeded")
        return evaluate_policy(policy, ctx)

    def replace(self, policy: Policy) -> int:
        """Atomically swap in a new policy. Returns the new version."""
        with self._lock:
            self.policy = policy
            self.version += 1
            return self.version

    def reload(self) -> int:
        """Reload from the source file. On error the old policy stays active."""
        if self.source is None:
            raise PolicyError("engine has no source file to reload from")
        return self.replace(load_policy_file(self.source))

    def forget_run(self, run_id: str) -> None:
        with self._lock:
            self._limiter.forget(run_id)
