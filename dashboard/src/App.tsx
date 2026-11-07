import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, HarnessApi } from "./api";
import { EventDrawer } from "./components/EventDrawer";
import { PolicyPanel } from "./components/PolicyPanel";
import { ALL_RUNS, RunsSidebar } from "./components/RunsSidebar";
import { TokenGate } from "./components/TokenGate";
import { TrafficTable } from "./components/TrafficTable";
import {
  DEFAULT_RULE,
  describe,
  filterEvents,
  matchedRule,
  mergeEvents,
  stats,
  type DecisionFilter,
} from "./eventStore";
import type { HarnessEvent, PolicyDoc, Run } from "./types";
import { useEventStream, type StreamStatus } from "./useEventStream";

const API_BASE = import.meta.env.VITE_HARNESS_API ?? "http://127.0.0.1:8000";
const TOKEN_KEY = "agent-harness.operator-token";
const HISTORY_RUNS = 5; // load stored events for the newest runs on start

export default function App() {
  const [token, setToken] = useState<string | null>(() => sessionStorage.getItem(TOKEN_KEY));
  const [notice, setNotice] = useState<string | null>(null);
  const api = useMemo(() => (token ? new HarnessApi(API_BASE, token) : null), [token]);

  const signOut = useCallback((message: string | null = null) => {
    sessionStorage.removeItem(TOKEN_KEY);
    setNotice(message);
    setToken(null);
  }, []);

  if (!api) {
    return (
      <TokenGate
        apiBase={API_BASE}
        notice={notice}
        onAuthenticated={(t) => {
          sessionStorage.setItem(TOKEN_KEY, t);
          setNotice(null);
          setToken(t);
        }}
      />
    );
  }
  return (
    <Dashboard
      api={api}
      onSignOut={() => signOut()}
      onAuthError={() => signOut("The harness rejected the saved token (it changes each time `harness serve` restarts). Paste the new one.")}
    />
  );
}

const CONNECTION_LABEL: Record<StreamStatus, string> = {
  connecting: "Connecting…",
  open: "Live",
  reconnecting: "Reconnecting…",
};

const FILTERS: { value: DecisionFilter; label: string }[] = [
  { value: "all", label: "All" },
  { value: "allowed", label: "Allowed" },
  { value: "blocked", label: "Blocked" },
];

interface DashboardProps {
  api: HarnessApi;
  onSignOut: () => void;
  onAuthError: () => void;
}

function Dashboard({ api, onSignOut, onAuthError }: DashboardProps) {
  const [runs, setRuns] = useState<Run[]>([]);
  const [events, setEvents] = useState<HarnessEvent[]>([]);
  const [policy, setPolicy] = useState<PolicyDoc | null>(null);
  const [selectedRun, setSelectedRun] = useState<string>(ALL_RUNS);
  const [filter, setFilter] = useState<DecisionFilter>("all");
  const [selectedEventId, setSelectedEventId] = useState<string | null>(null);
  const [autoScroll, setAutoScroll] = useState(true);
  const [stopping, setStopping] = useState<ReadonlySet<string>>(new Set());
  const [reloading, setReloading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [announcement, setAnnouncement] = useState("");
  const historyLoaded = useRef(new Set<string>());

  const handleError = useCallback(
    (e: unknown) => {
      if (e instanceof ApiError && e.status === 401) onAuthError();
      else setError(e instanceof Error ? e.message : String(e));
    },
    [onAuthError],
  );

  const refreshRuns = useCallback(async () => {
    try {
      setRuns(await api.runs());
    } catch (e) {
      handleError(e);
    }
  }, [api, handleError]);

  const refreshPolicy = useCallback(async () => {
    try {
      setPolicy(await api.policy());
    } catch (e) {
      handleError(e);
    }
  }, [api, handleError]);

  const loadHistory = useCallback(
    async (runId: string, force = false) => {
      if (!force && historyLoaded.current.has(runId)) return;
      historyLoaded.current.add(runId);
      try {
        const history = await api.events(runId);
        setEvents((prev) => mergeEvents(prev, history));
      } catch (e) {
        historyLoaded.current.delete(runId);
        handleError(e);
      }
    },
    [api, handleError],
  );

  const onEvents = useCallback(
    (batch: HarnessEvent[]) => {
      setEvents((prev) => mergeEvents(prev, batch));
      void refreshRuns(); // keep status and allow/block counts current
      if (batch.some((e) => e.kind === "policy_reloaded")) void refreshPolicy();
      const notable = [...batch]
        .reverse()
        .find((e) => (e.kind === "request" && e.decision === "blocked") || e.kind === "run_stopped");
      if (notable) setAnnouncement(describe(notable));
    },
    [refreshRuns, refreshPolicy],
  );

  const wsUrl = useMemo(() => api.wsUrl(), [api]);
  const connection = useEventStream(wsUrl, onEvents);

  // On every (re)connect, resync anything that may have happened while disconnected.
  useEffect(() => {
    if (connection !== "open") return;
    void refreshRuns();
    void refreshPolicy();
    for (const runId of historyLoaded.current) void loadHistory(runId, true);
  }, [connection, refreshRuns, refreshPolicy, loadHistory]);

  // Initial load (also covers the case where the stream never connects).
  useEffect(() => {
    void refreshRuns();
    void refreshPolicy();
  }, [refreshRuns, refreshPolicy]);

  useEffect(() => {
    for (const run of runs.slice(0, HISTORY_RUNS)) void loadHistory(run.id);
  }, [runs, loadHistory]);

  // Safety net for counts if the stream is down.
  useEffect(() => {
    const timer = setInterval(() => void refreshRuns(), 5000);
    return () => clearInterval(timer);
  }, [refreshRuns]);

  const selectRun = useCallback(
    (runId: string) => {
      setSelectedRun(runId);
      setSelectedEventId(null);
      if (runId !== ALL_RUNS) void loadHistory(runId);
    },
    [loadHistory],
  );

  const stopRun = useCallback(
    async (runId: string) => {
      setStopping((s) => new Set(s).add(runId));
      try {
        await api.stop(runId, "stopped from the dashboard");
        await refreshRuns();
      } catch (e) {
        handleError(e);
      } finally {
        setStopping((s) => {
          const next = new Set(s);
          next.delete(runId);
          return next;
        });
      }
    },
    [api, refreshRuns, handleError],
  );

  const reloadPolicy = useCallback(async () => {
    setReloading(true);
    try {
      await api.reloadPolicy();
      await refreshPolicy();
    } catch (e) {
      handleError(e);
    } finally {
      setReloading(false);
    }
  }, [api, refreshPolicy, handleError]);

  const closeDrawer = useCallback(() => setSelectedEventId(null), []);

  const runFilter = selectedRun === ALL_RUNS ? null : selectedRun;
  const visible = useMemo(() => filterEvents(events, runFilter, filter), [events, runFilter, filter]);
  const counts = useMemo(() => stats(filterEvents(events, runFilter, "all")), [events, runFilter]);
  const selectedEvent = useMemo(() => events.find((e) => e.id === selectedEventId) ?? null, [events, selectedEventId]);
  const highlight = selectedEvent ? matchedRule(selectedEvent) : null;
  const highlightedRule = policy?.policy.rules.find((r) => r.id === highlight) ?? null;
  const currentRun = runs.find((r) => r.id === selectedRun);

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="logo" aria-hidden="true">
            ⛨
          </span>
          <div>
            <h1>Agent Harness</h1>
            <p className="tagline">Every agent request goes through the gateway</p>
          </div>
        </div>
        <div className="topbar-right">
          <span className={`connection connection-${connection}`} role="status">
            <span aria-hidden="true">●</span> {CONNECTION_LABEL[connection]}
          </span>
          <button type="button" className="button button-quiet" onClick={onSignOut}>
            Sign out
          </button>
        </div>
      </header>

      {error && (
        <div className="banner" role="alert">
          <span>{error}</span>
          <button type="button" className="icon-button" aria-label="Dismiss error" onClick={() => setError(null)}>
            ✕
          </button>
        </div>
      )}

      {/* Screen readers hear blocks and stops as they happen. */}
      <p className="sr-only" aria-live="polite">
        {announcement}
      </p>

      <div className="layout">
        <RunsSidebar runs={runs} selected={selectedRun} stopping={stopping} onSelect={selectRun} onStop={stopRun} />

        <main className="panel main" aria-labelledby="traffic-title">
          <div className="panel-head">
            <h2 id="traffic-title" className="panel-title">
              Live traffic{" "}
              <span className="muted">{currentRun ? `· run ${currentRun.id}` : "· all runs"}</span>
            </h2>
            <div className="controls">
              <div className="segmented" role="group" aria-label="Filter by decision">
                {FILTERS.map((f) => (
                  <button
                    key={f.value}
                    type="button"
                    aria-pressed={filter === f.value}
                    onClick={() => setFilter(f.value)}
                  >
                    {f.label}
                  </button>
                ))}
              </div>
              <label className="toggle">
                <input type="checkbox" checked={autoScroll} onChange={(e) => setAutoScroll(e.target.checked)} />
                Auto-scroll
              </label>
            </div>
          </div>

          <ul className="stats" aria-label="Request totals">
            <li>
              <span className="stat-value">{counts.total}</span> requests
            </li>
            <li className="stat-allowed">
              <span className="stat-value">{counts.allowed}</span> allowed
            </li>
            <li className="stat-blocked">
              <span className="stat-value">{counts.blocked}</span> blocked
            </li>
            <li className="stat-pending">
              <span className="stat-value">{counts.pending}</span> in flight
            </li>
          </ul>

          <TrafficTable
            events={visible}
            selectedId={selectedEventId}
            showRun={selectedRun === ALL_RUNS}
            autoScroll={autoScroll}
            onSelect={setSelectedEventId}
          />
        </main>

        <PolicyPanel policy={policy} highlight={highlight} reloading={reloading} onReload={reloadPolicy} />

        {selectedEvent && (
          <EventDrawer
            event={selectedEvent}
            rule={highlightedRule}
            usedDefault={highlight === DEFAULT_RULE}
            defaultAction={policy?.policy.default ?? null}
            onClose={closeDrawer}
          />
        )}
      </div>
    </div>
  );
}
