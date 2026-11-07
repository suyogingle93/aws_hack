import type { HarnessEvent, PolicyDoc, Run } from "../types";

let n = 0;

export function ev(over: Partial<HarnessEvent> = {}): HarnessEvent {
  n += 1;
  return {
    id: `e${n}`,
    run_id: "r1",
    ts: "2026-11-07T21:00:00.000000Z",
    kind: "request",
    method: "GET",
    host: "api.github.com",
    path: "/zen",
    tool: null,
    decision: "allowed",
    rule_id: "allow-github-read",
    reason: "allowed by rule 'allow-github-read'",
    status_code: 200,
    headers: {},
    ...over,
  };
}

export function run(over: Partial<Run> = {}): Run {
  return {
    id: "r1",
    command: ["./venv/bin/python", "backend/demo/demo_agent.py"],
    cwd: "/tmp",
    status: "running",
    pid: 1234,
    started_at: "2026-11-07T21:00:00Z",
    ended_at: null,
    exit_code: null,
    allowed: 3,
    blocked: 2,
    ...over,
  };
}

export const POLICY: PolicyDoc = {
  version: 1,
  policy: {
    default: "deny",
    rate_limit: { requests_per_minute: 120 },
    rules: [
      { id: "allow-github-read", action: "allow", host: "api.github.com", methods: ["GET"], description: "Read-only GitHub API" },
      { id: "block-paste-sites", action: "deny", host: "*pastebin.com", terminate_on_violation: true },
    ],
  },
};
