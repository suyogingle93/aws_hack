import type { HarnessEvent, PolicyDoc, Run } from "./types";

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status; // 0 = network error
  }
}

/** Thin client for the harness control API. All calls send the operator token. */
export class HarnessApi {
  readonly baseUrl: string;
  readonly #token: string;

  constructor(baseUrl: string, token: string) {
    this.baseUrl = baseUrl.replace(/\/+$/, "");
    this.#token = token;
  }

  async #call<T>(method: string, path: string, body?: unknown): Promise<T> {
    let res: Response;
    try {
      res = await fetch(this.baseUrl + path, {
        method,
        headers: {
          Authorization: `Bearer ${this.#token}`,
          ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
        },
        body: body !== undefined ? JSON.stringify(body) : undefined,
      });
    } catch {
      throw new ApiError(0, `Cannot reach the harness at ${this.baseUrl}. Is \`harness serve\` running?`);
    }
    if (!res.ok) {
      let detail = res.statusText || `HTTP ${res.status}`;
      try {
        const data = await res.json();
        if (typeof data?.detail === "string") detail = data.detail;
      } catch {
        // keep statusText
      }
      throw new ApiError(res.status, detail);
    }
    return (await res.json()) as T;
  }

  runs(): Promise<Run[]> {
    return this.#call("GET", "/runs");
  }

  events(runId: string): Promise<HarnessEvent[]> {
    return this.#call("GET", `/runs/${encodeURIComponent(runId)}/events?limit=5000`);
  }

  stop(runId: string, reason: string): Promise<{ stopped: boolean; run: Run }> {
    return this.#call("POST", `/runs/${encodeURIComponent(runId)}/stop`, { reason });
  }

  policy(): Promise<PolicyDoc> {
    return this.#call("GET", "/policy");
  }

  reloadPolicy(): Promise<{ version: number }> {
    return this.#call("POST", "/policy/reload");
  }

  /** Browsers cannot set headers on WebSockets, so the token goes in the query string. */
  wsUrl(): string {
    const url = new URL(this.baseUrl);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    url.pathname = "/ws";
    url.searchParams.set("token", this.#token);
    return url.toString();
  }
}
