import type { HarnessEvent } from "./types";

export const MAX_EVENTS = 5000;
export const DEFAULT_RULE = "__default__";

export type DecisionFilter = "all" | "allowed" | "blocked";
export type RequestState = "allowed" | "blocked" | "pending";

const time = (e: HarnessEvent) => Date.parse(e.ts);

/**
 * Merge new events into the list. The gateway sends an allowed request twice (when it is
 * forwarded, then again with its status code), so events are upserted by id. The result is
 * ordered by timestamp (history from REST and live events from the WebSocket can interleave)
 * and capped to the newest `cap` events.
 */
export function mergeEvents(current: HarnessEvent[], incoming: HarnessEvent[], cap = MAX_EVENTS): HarnessEvent[] {
  if (incoming.length === 0) return current;
  const index = new Map(current.map((e, i) => [e.id, i]));
  const next = current.slice();
  for (const event of incoming) {
    const i = index.get(event.id);
    if (i === undefined) {
      index.set(event.id, next.length);
      next.push(event);
    } else {
      // Never let an older copy (e.g. history fetched late) erase a status we already have.
      next[i] = { ...next[i], ...event, status_code: event.status_code ?? next[i].status_code };
    }
  }
  next.sort((a, b) => time(a) - time(b)); // stable, so equal timestamps keep arrival order
  return next.length > cap ? next.slice(next.length - cap) : next;
}

export function requestState(e: HarnessEvent): RequestState {
  if (e.decision === "blocked") return "blocked";
  if (e.decision === "allowed" && e.status_code != null) return "allowed";
  return "pending"; // forwarded, response not back yet
}

/** Events for one run (or all runs when runId is null), narrowed by decision. */
export function filterEvents(events: HarnessEvent[], runId: string | null, decision: DecisionFilter): HarnessEvent[] {
  return events.filter((e) => {
    if (runId !== null && e.run_id !== runId) return false;
    if (decision === "all") return true;
    return e.kind === "request" && e.decision === decision;
  });
}

/** Which policy rule produced this decision: a rule id, DEFAULT_RULE, or null. */
export function matchedRule(e: HarnessEvent): string | null {
  if (e.kind !== "request") return null;
  if (e.rule_id) return e.rule_id;
  if (e.decision && e.reason?.startsWith("no rule matched")) return DEFAULT_RULE;
  return null;
}

export interface Stats {
  total: number;
  allowed: number;
  blocked: number;
  pending: number;
}

export function stats(events: HarnessEvent[]): Stats {
  const s: Stats = { total: 0, allowed: 0, blocked: 0, pending: 0 };
  for (const e of events) {
    if (e.kind !== "request") continue;
    s.total += 1;
    s[requestState(e)] += 1;
  }
  return s;
}

export function destination(e: HarnessEvent): string {
  return `${e.host ?? ""}${e.path ?? ""}`;
}

/** One sentence for screen-reader announcements and lifecycle rows. */
export function describe(e: HarnessEvent): string {
  switch (e.kind) {
    case "request":
      return e.decision === "blocked"
        ? `Blocked ${e.method ?? ""} ${e.host ?? ""}: ${e.rule_id ?? e.reason ?? "policy"}`
        : `${e.method ?? ""} ${e.host ?? ""}`;
    case "run_started":
      return `Run ${e.run_id} started`;
    case "run_stopped":
      return `Run ${e.run_id} stopped (${e.reason ?? "stopped"})`;
    case "run_finished":
      return `Run ${e.run_id} finished (${e.reason ?? ""})`;
    case "policy_reloaded":
      return e.reason ?? "Policy reloaded";
  }
}

export function formatTime(ts: string): string {
  const d = new Date(ts);
  return Number.isNaN(d.getTime()) ? "--:--:--" : d.toLocaleTimeString([], { hour12: false });
}

/** "./venv/bin/python backend/demo/demo_agent.py" -> "python demo_agent.py" */
export function commandLabel(command: string[]): string {
  return command.map((part) => part.split("/").pop() || part).join(" ");
}
