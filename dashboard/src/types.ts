// Shapes returned by the harness control API (backend/harness/api.py).

export type Decision = "allowed" | "blocked";
export type EventKind = "request" | "run_started" | "run_stopped" | "run_finished" | "policy_reloaded";

export interface HarnessEvent {
  id: string;
  run_id: string;
  ts: string;
  kind: EventKind;
  method: string | null;
  host: string | null;
  path: string | null;
  tool: string | null;
  decision: Decision | null;
  rule_id: string | null;
  reason: string | null;
  status_code: number | null;
  headers: Record<string, string>; // already redacted by the gateway
}

export type RunStatus = "running" | "stopped" | "finished";

export interface Run {
  id: string;
  command: string[];
  cwd: string;
  status: RunStatus;
  pid: number | null;
  started_at: string;
  ended_at: string | null;
  exit_code: number | null;
  allowed: number;
  blocked: number;
}

export interface Rule {
  id: string;
  action: "allow" | "deny";
  host?: string;
  path?: string;
  methods?: string[];
  mcp_tool?: string;
  terminate_on_violation?: boolean;
  description?: string;
}

export interface PolicyDoc {
  version: number;
  policy: {
    default: "allow" | "deny";
    rate_limit?: { requests_per_minute: number };
    rules: Rule[];
  };
}
