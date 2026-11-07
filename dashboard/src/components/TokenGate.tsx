import { useState, type FormEvent } from "react";
import { ApiError, HarnessApi } from "../api";

interface Props {
  apiBase: string;
  notice: string | null;
  onAuthenticated: (token: string) => void;
}

export function TokenGate({ apiBase, notice, onAuthenticated }: Props) {
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    const candidate = token.trim();
    if (!candidate) return;
    setChecking(true);
    setError(null);
    try {
      await new HarnessApi(apiBase, candidate).runs();
      onAuthenticated(candidate);
    } catch (err) {
      setError(
        err instanceof ApiError && err.status === 401
          ? "That token was rejected. Copy the current one from the harness output."
          : err instanceof Error
            ? err.message
            : String(err),
      );
    } finally {
      setChecking(false);
    }
  }

  return (
    <main className="gate">
      <form className="panel gate-card" onSubmit={submit} aria-labelledby="gate-title">
        <h1 id="gate-title">Agent Harness</h1>
        <p className="muted">Connect to the control API at <code>{apiBase}</code>.</p>
        {notice && (
          <p className="notice" role="status">
            {notice}
          </p>
        )}
        <label htmlFor="token">Operator token</label>
        <input
          id="token"
          type="password"
          autoComplete="off"
          spellCheck={false}
          value={token}
          onChange={(e) => setToken(e.target.value)}
          aria-describedby="token-help"
          aria-invalid={error ? true : undefined}
          required
        />
        <p id="token-help" className="hint">
          Printed by <code>harness serve</code>, or run <code>cat ~/.agent-harness/operator_token</code>.
        </p>
        {error && (
          <p className="error-text" role="alert">
            {error}
          </p>
        )}
        <button type="submit" className="button button-primary" disabled={checking || !token.trim()}>
          {checking ? "Checking…" : "Connect"}
        </button>
      </form>
    </main>
  );
}
