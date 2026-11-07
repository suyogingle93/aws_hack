import { useEffect, useRef, useState } from "react";
import type { HarnessEvent } from "./types";

export type StreamStatus = "connecting" | "open" | "reconnecting";

/**
 * Subscribe to the harness event stream. Reconnects with backoff (1s, 2s, 4s ... 10s) and
 * delivers events in small batches so a burst of traffic causes one render, not hundreds.
 */
export function useEventStream(
  url: string | null,
  onEvents: (batch: HarnessEvent[]) => void,
  flushMs = 100,
): StreamStatus {
  const [status, setStatus] = useState<StreamStatus>("connecting");
  const handler = useRef(onEvents);

  useEffect(() => {
    handler.current = onEvents;
  }, [onEvents]);

  useEffect(() => {
    if (!url) return;
    let ws: WebSocket | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    let flushTimer: ReturnType<typeof setTimeout> | undefined;
    let buffer: HarnessEvent[] = [];
    let failures = 0;
    let disposed = false;

    const flush = () => {
      flushTimer = undefined;
      const batch = buffer;
      buffer = [];
      if (batch.length > 0) handler.current(batch);
    };

    const connect = () => {
      setStatus(failures === 0 ? "connecting" : "reconnecting");
      ws = new WebSocket(url);
      ws.onopen = () => {
        failures = 0;
        setStatus("open");
      };
      ws.onmessage = (message: MessageEvent) => {
        try {
          buffer.push(JSON.parse(String(message.data)) as HarnessEvent);
        } catch {
          return; // ignore anything that is not an event
        }
        flushTimer ??= setTimeout(flush, flushMs);
      };
      ws.onclose = () => {
        if (disposed) return;
        failures += 1;
        setStatus("reconnecting");
        retryTimer = setTimeout(connect, Math.min(1000 * 2 ** (failures - 1), 10_000));
      };
    };

    connect();
    return () => {
      disposed = true;
      clearTimeout(retryTimer);
      clearTimeout(flushTimer);
      if (ws) {
        ws.onclose = null;
        ws.onmessage = null;
        ws.close();
      }
    };
  }, [url, flushMs]);

  return status;
}
