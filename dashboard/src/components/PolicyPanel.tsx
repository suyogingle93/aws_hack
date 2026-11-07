import { DEFAULT_RULE } from "../eventStore";
import type { PolicyDoc, Rule } from "../types";

interface Props {
  policy: PolicyDoc | null;
  highlight: string | null;
  reloading: boolean;
  onReload: () => void;
}

function Matchers({ rule }: { rule: Rule }) {
  const items: [string, string][] = [];
  if (rule.host) items.push(["host", rule.host]);
  if (rule.path) items.push(["path", rule.path]);
  if (rule.methods?.length) items.push(["methods", rule.methods.join(", ")]);
  if (rule.mcp_tool) items.push(["MCP tool", rule.mcp_tool]);
  return (
    <dl className="matchers">
      {items.map(([k, v]) => (
        <div key={k} className="matcher">
          <dt>{k}</dt>
          <dd>
            <code>{v}</code>
          </dd>
        </div>
      ))}
    </dl>
  );
}

export function PolicyPanel({ policy, highlight, reloading, onReload }: Props) {
  return (
    <section className="panel policy" aria-labelledby="policy-title">
      <div className="panel-head">
        <h2 id="policy-title" className="panel-title">
          Policy {policy && <span className="muted">v{policy.version}</span>}
        </h2>
        <button type="button" className="button" onClick={onReload} disabled={reloading}>
          {reloading ? "Reloading…" : "Reload"}
        </button>
      </div>
      {!policy ? (
        <p className="muted">Loading policy…</p>
      ) : (
        <>
          <p className="hint">Deny rules win over allow rules. Select a request to see which rule decided it.</p>
          <ul className="rules">
            {policy.policy.rules.map((rule) => {
              const matched = highlight === rule.id;
              return (
                <li key={rule.id} className={`rule rule-${rule.action} ${matched ? "is-matched" : ""}`} aria-current={matched ? "true" : undefined}>
                  <div className="rule-head">
                    <span className={`action action-${rule.action}`}>{rule.action === "allow" ? "Allow" : "Deny"}</span>
                    <code className="rule-id">{rule.id}</code>
                    {matched && <span className="matched-tag">matched</span>}
                  </div>
                  {rule.description && <p className="rule-desc">{rule.description}</p>}
                  <Matchers rule={rule} />
                  {rule.terminate_on_violation && <p className="terminate">■ Stops the run on violation</p>}
                </li>
              );
            })}
            <li
              className={`rule rule-default ${highlight === DEFAULT_RULE ? "is-matched" : ""}`}
              aria-current={highlight === DEFAULT_RULE ? "true" : undefined}
            >
              <div className="rule-head">
                <span className={`action action-${policy.policy.default === "allow" ? "allow" : "deny"}`}>
                  {policy.policy.default === "allow" ? "Allow" : "Deny"}
                </span>
                <span className="rule-id">Everything else (default)</span>
                {highlight === DEFAULT_RULE && <span className="matched-tag">matched</span>}
              </div>
            </li>
          </ul>
          {policy.policy.rate_limit && (
            <p className="hint">Rate limit: {policy.policy.rate_limit.requests_per_minute} requests per minute per run.</p>
          )}
        </>
      )}
    </section>
  );
}
