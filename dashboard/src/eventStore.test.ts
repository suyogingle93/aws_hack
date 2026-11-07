import { describe, expect, it } from "vitest";
import { commandLabel, DEFAULT_RULE, filterEvents, matchedRule, mergeEvents, requestState, stats } from "./eventStore";
import { ev } from "./test/factories";

describe("mergeEvents", () => {
  it("upserts by id so the response update replaces the in-flight copy in place", () => {
    const sent = ev({ id: "a", status_code: null });
    const other = ev({ id: "b", ts: "2026-11-07T21:00:01Z" });
    const merged = mergeEvents(mergeEvents([], [sent, other]), [{ ...sent, status_code: 200 }]);
    expect(merged.map((e) => e.id)).toEqual(["a", "b"]);
    expect(merged[0].status_code).toBe(200);
  });

  it("never lets a stale copy erase a status code it already has", () => {
    const done = ev({ id: "a", status_code: 200 });
    const merged = mergeEvents([done], [{ ...done, status_code: null }]);
    expect(merged[0].status_code).toBe(200);
  });

  it("orders by real time even when the timestamp formats differ", () => {
    // pydantic omits the fraction on whole seconds, so string order would be wrong here.
    const late = ev({ id: "late", ts: "2026-11-07T21:00:01Z" });
    const early = ev({ id: "early", ts: "2026-11-07T21:00:00.500000Z" });
    expect(mergeEvents([], [late, early]).map((e) => e.id)).toEqual(["early", "late"]);
  });

  it("caps to the newest events", () => {
    const many = Array.from({ length: 10 }, (_, i) => ev({ id: `x${i}`, ts: `2026-11-07T21:00:0${i}Z` }));
    expect(mergeEvents([], many, 3).map((e) => e.id)).toEqual(["x7", "x8", "x9"]);
  });

  it("returns the same array when there is nothing new", () => {
    const list = [ev()];
    expect(mergeEvents(list, [])).toBe(list);
  });
});

describe("helpers", () => {
  it("classifies requests as allowed, blocked, or in flight", () => {
    expect(requestState(ev())).toBe("allowed");
    expect(requestState(ev({ status_code: null }))).toBe("pending");
    expect(requestState(ev({ decision: "blocked", status_code: 403 }))).toBe("blocked");
  });

  it("filters by run and decision, keeping lifecycle rows only in the unfiltered view", () => {
    const events = [
      ev({ id: "ok" }),
      ev({ id: "no", decision: "blocked" }),
      ev({ id: "other", run_id: "r2" }),
      ev({ id: "stop", kind: "run_stopped", decision: null }),
    ];
    expect(filterEvents(events, "r1", "all").map((e) => e.id)).toEqual(["ok", "no", "stop"]);
    expect(filterEvents(events, "r1", "blocked").map((e) => e.id)).toEqual(["no"]);
    expect(filterEvents(events, null, "allowed").map((e) => e.id)).toEqual(["ok", "other"]);
  });

  it("finds the rule that decided, including the default action", () => {
    expect(matchedRule(ev())).toBe("allow-github-read");
    expect(matchedRule(ev({ decision: "blocked", rule_id: null, reason: "no rule matched; default deny" }))).toBe(DEFAULT_RULE);
    expect(matchedRule(ev({ decision: "blocked", rule_id: null, reason: "rate limit exceeded" }))).toBeNull();
    expect(matchedRule(ev({ kind: "run_stopped" }))).toBeNull();
  });

  it("counts only requests", () => {
    const s = stats([ev(), ev({ decision: "blocked" }), ev({ status_code: null }), ev({ kind: "run_started" })]);
    expect(s).toEqual({ total: 3, allowed: 1, blocked: 1, pending: 1 });
  });

  it("shortens commands for display", () => {
    expect(commandLabel(["./venv/bin/python", "backend/demo/demo_agent.py"])).toBe("python demo_agent.py");
  });
});
