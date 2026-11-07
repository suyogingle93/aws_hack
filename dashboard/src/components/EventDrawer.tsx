import { useEffect, useRef } from "react";
import { destination, formatTime, requestState } from "../eventStore";
import type { HarnessEvent, Rule } from "../types";
import { DecisionBadge } from "./TrafficTable";

interface Props {
  event: HarnessEvent;
  rule: Rule | null;
  usedDefault: boolean;
  defaultAction: string | null;
  onClose: () => void;
}

export function EventDrawer({ event, rule, usedDefault, defaultAction, onClose }: Props) {
  const heading = useRef<HTMLHeadingElement>(null);

  // Move focus into the drawer on open, and give it back to whatever opened it on close.
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    heading.current?.focus();
    return () => opener?.focus?.();
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const headers = Object.entries(event.headers ?? {});

  return (
    <aside className="drawer" role="dialog" aria-modal="false" aria-labelledby="drawer-title">
      <div className="drawer-head">
        <h2 id="drawer-title" className="panel-title" tabIndex={-1} ref={heading}>
          Request details
        </h2>
        <button type="button" className="icon-button" aria-label="Close details" onClick={onClose}>
          ✕
        </button>
      </div>

      <dl className="details">
        <dt>Decision</dt>
        <dd>
          <DecisionBadge state={requestState(event)} />
        </dd>
        <dt>Reason</dt>
        <dd>{event.reason ?? "—"}</dd>
        <dt>Rule</dt>
        <dd>
          {rule ? (
            <>
              <code>{rule.id}</code>
              {rule.description && <div className="muted">{rule.description}</div>}
            </>
          ) : usedDefault ? (
            <>Default action ({defaultAction ?? "deny"}): no rule matched</>
          ) : (
            "—"
          )}
        </dd>
        <dt>Request</dt>
        <dd className="mono break">
          {event.method} {destination(event)}
        </dd>
        {event.tool && (
          <>
            <dt>MCP tool</dt>
            <dd>
              <code>{event.tool}</code>
            </dd>
          </>
        )}
        <dt>HTTP status</dt>
        <dd className="mono">{event.status_code ?? "waiting for response"}</dd>
        <dt>Run</dt>
        <dd>
          <code>{event.run_id}</code>
        </dd>
        <dt>Time</dt>
        <dd className="mono">{formatTime(event.ts)}</dd>
      </dl>

      <h3 className="subhead">Headers</h3>
      <p className="hint">Credentials are redacted by the gateway before they are logged or shown here.</p>
      {headers.length === 0 ? (
        <p className="muted">No headers recorded.</p>
      ) : (
        <table className="headers">
          <caption className="sr-only">Request headers</caption>
          <tbody>
            {headers.map(([name, value]) => (
              <tr key={name}>
                <th scope="row">{name}</th>
                <td className={value.includes("[REDACTED]") ? "redacted" : undefined}>{value}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </aside>
  );
}
