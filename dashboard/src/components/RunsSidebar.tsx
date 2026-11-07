import { commandLabel } from "../eventStore";
import type { Run, RunStatus } from "../types";

export const ALL_RUNS = "__all__";

const STATUS_LABEL: Record<RunStatus, string> = { running: "Running", stopped: "Stopped", finished: "Finished" };

export function StatusBadge({ status }: { status: RunStatus }) {
  return (
    <span className={`status status-${status}`}>
      <span aria-hidden="true">●</span> {STATUS_LABEL[status]}
    </span>
  );
}

interface Props {
  runs: Run[];
  selected: string;
  stopping: ReadonlySet<string>;
  onSelect: (runId: string) => void;
  onStop: (runId: string) => void;
}

export function RunsSidebar({ runs, selected, stopping, onSelect, onStop }: Props) {
  return (
    <nav className="panel sidebar" aria-labelledby="runs-title">
      <h2 id="runs-title" className="panel-title">
        Runs
      </h2>
      <ul className="run-list">
        <li className={`run-item ${selected === ALL_RUNS ? "is-selected" : ""}`}>
          <button type="button" className="run-select" aria-pressed={selected === ALL_RUNS} onClick={() => onSelect(ALL_RUNS)}>
            <span className="run-head">All runs</span>
          </button>
        </li>
        {runs.length === 0 && (
          <li className="empty">
            No runs yet. Start one with <code>harness run -- &lt;agent&gt;</code>
          </li>
        )}
        {runs.map((run) => {
          const isStopping = stopping.has(run.id);
          return (
            <li key={run.id} className={`run-item ${selected === run.id ? "is-selected" : ""}`}>
              <button
                type="button"
                className="run-select"
                aria-pressed={selected === run.id}
                onClick={() => onSelect(run.id)}
              >
                <span className="run-head">
                  <code className="run-id">{run.id}</code>
                  <StatusBadge status={run.status} />
                </span>
                <span className="run-command" title={run.command.join(" ")}>
                  {commandLabel(run.command)}
                </span>
                <span className="run-counts">
                  <span className="count count-allowed">✓ {run.allowed} allowed</span>
                  <span className="count count-blocked">✕ {run.blocked} blocked</span>
                </span>
              </button>
              {run.status === "running" && (
                <button
                  type="button"
                  className="stop-button"
                  aria-label={`Stop run ${run.id}`}
                  disabled={isStopping}
                  onClick={() => onStop(run.id)}
                >
                  {isStopping ? "Stopping…" : "■ Stop"}
                </button>
              )}
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
