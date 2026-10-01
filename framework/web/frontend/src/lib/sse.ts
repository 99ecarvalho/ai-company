/**
 * Thin EventSource wrapper used by reactive stores that used to poll.
 *
 * Philosophy: SSE is a *trigger* — the handler receives the payload and decides
 * what to do (refetch, append, etc). Auto-reconnect comes from the native browser;
 * on `onopen` the caller can re-hydrate state to cover events lost
 * during downtime (the `/api/events` backend does not implement Last-Event-ID).
 *
 * Uses EventSource's implicit `credentials: same-origin` — the `agf_session`
 * cookie goes through without extra config.
 */

export interface SSEClientOptions<T> {
  /** Absolute or relative URL of the SSE endpoint. */
  url: string;
  /** Handler for each received event. Payload is already-parsed JSON. */
  onMessage: (data: T) => void;
  /** Fired on each successful (re)connection. Use it to re-hydrate state. */
  onOpen?: () => void;
  /** Fired on connection error (EventSource itself auto-reconnects). */
  onError?: (ev: Event) => void;
}

export interface SSEClient {
  /** Closes the connection; idempotent. */
  close(): void;
}

export function createSSEClient<T = unknown>(opts: SSEClientOptions<T>): SSEClient {
  if (typeof EventSource === 'undefined') {
    return { close: () => { /* no-op SSR */ } };
  }
  let closed = false;
  let es: EventSource | null = null;

  const open = () => {
    if (closed) return;
    es = new EventSource(opts.url);
    es.onopen = () => { opts.onOpen?.(); };
    es.onmessage = (msg) => {
      try {
        const data = JSON.parse(msg.data) as T;
        opts.onMessage(data);
      } catch {
        /* ignore malformed payload */
      }
    };
    es.onerror = (ev) => { opts.onError?.(ev); };
  };

  open();

  return {
    close() {
      closed = true;
      if (es) {
        es.close();
        es = null;
      }
    }
  };
}

/**
 * Refresh coalescing: if a refresh is in flight, mark a trailing one to run
 * right after. Avoids losing the "last" event in a burst (the problem with a
 * plain `inFlight` lock) while not firing N in parallel.
 */
export function coalesceRefresh(fn: () => Promise<void>): () => void {
  let inFlight = false;
  let pending = false;
  const run = async () => {
    if (inFlight) {
      pending = true;
      return;
    }
    inFlight = true;
    try {
      await fn();
    } finally {
      inFlight = false;
      if (pending) {
        pending = false;
        // Trailing tick with a small delay to group nearby events.
        setTimeout(run, 50);
      }
    }
  };
  return run;
}
