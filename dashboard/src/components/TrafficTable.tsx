import { useEffect, useRef } from "react";
import { describe, destination, formatTime, requestState, type RequestState } from "../eventStore";
import type { HarnessEvent } from "../types";

const STATE_LABEL: Record<RequestState, { icon: string; text: string }> = {
  allowed: { icon: "✓", text: "Allowed" },
  blocked: { icon: "✕", text: "Blocked" },
  pending: { icon: "…", text: "In flight" },
};

export function DecisionBadge({ state }: { state: RequestState }) {
  const { icon, text } = STATE_LABEL[state];
  return (
    <span className={`decision decision-${state}`}>
      <span aria-hidden="true">{icon}</span> {text}
    </span>
  );
}

const LIFECYCLE: Record<string, { icon: string; label: string }> = {
  run_started: { icon: "▶", label: "Run started" },
  run_stopped: { icon: "■", label: "Run stopped" },
  run_finished: { icon: "⏹", label: "Run finished" },
  policy_reloaded: { icon: "↻", label: "Policy" },
};

interface Props {
  events: HarnessEvent[];
  selectedId: string | null;
  showRun: boolean;
  autoScroll: boolean;
  onSelect: (eventId: string) => void;
}

export function TrafficTable({ events, selectedId, showRun, autoScroll, onSelect }: Props) {
  const scroller = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = scroller.current;
    if (autoScroll && el) el.scrollTop = el.scrollHeight;
  }, [events, autoScroll]);

  const columns = showRun ? 7 : 6;

  return (
    // Focusable so keyboard users can scroll the region.
    <div className="table-scroll" ref={scroller} tabIndex={0} role="region" aria-label="Live traffic">
      <table className="traffic">
        <caption className="sr-only">Requests through the gateway, oldest first</caption>
        <thead>
          <tr>
            <th scope="col">Time</th>
            {showRun && <th scope="col">Run</th>}
            <th scope="col">Decision</th>
            <th scope="col">Method</th>
            <th scope="col">Destination</th>
            <th scope="col">Rule / reason</th>
            <th scope="col">Status</th>
          </tr>
        </thead>
        <tbody>
          {events.length === 0 && (
            <tr>
              <td colSpan={columns} className="table-empty">
                Waiting for traffic. Run an agent with <code>harness run -- &lt;agent&gt;</code>
              </td>
            </tr>
          )}
          {events.map((e) => {
            if (e.kind !== "request") {
              const info = LIFECYCLE[e.kind] ?? { icon: "•", label: e.kind };
              return (
                <tr key={e.id} className={`lifecycle lifecycle-${e.kind}`}>
                  <td className="mono">{formatTime(e.ts)}</td>
                  <td colSpan={columns - 1}>
                    <span aria-hidden="true">{info.icon}</span> <strong>{info.label}</strong>{" "}
                    <span className="muted">{e.kind === "policy_reloaded" ? describe(e) : `${e.run_id} · ${e.reason ?? ""}`}</span>
                  </td>
                </tr>
              );
            }
            const state = requestState(e);
            const dest = destination(e);
            return (
              <tr
                key={e.id}
                className={`row row-${state} ${selectedId === e.id ? "is-selected" : ""}`}
                onClick={() => onSelect(e.id)}
              >
                <td className="mono">{formatTime(e.ts)}</td>
                {showRun && (
                  <td>
                    <code>{e.run_id}</code>
                  </td>
                )}
                <td>
                  <DecisionBadge state={state} />
                </td>
                <td className="mono">{e.method}</td>
                <td className="dest">
                  <button
                    type="button"
                    className="link-button"
                    title={dest}
                    aria-pressed={selectedId === e.id}
                    onClick={(ev) => {
                      ev.stopPropagation();
                      onSelect(e.id);
                    }}
                  >
                    {dest}
                  </button>
                  {e.tool && <span className="tool-tag">tool: {e.tool}</span>}
                </td>
                <td className={state === "blocked" ? "reason-blocked" : "muted"}>{e.rule_id ?? e.reason}</td>
                <td className={`mono ${e.status_code && e.status_code >= 500 ? "status-error" : ""}`}>
                  {e.status_code ?? "—"}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
