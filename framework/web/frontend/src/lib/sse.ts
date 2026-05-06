/**
 * Thin EventSource wrapper used por stores reativos que antes polavam.
 *
 * Filosofia: SSE eh um *trigger* — handler recebe o payload e decide o que
 * fazer (refetch, append, etc). Auto-reconnect vem do browser nativo; no
 * `onopen` o caller pode re-hidratar estado pra cobrir eventos perdidos
 * durante downtime (o backend `/api/events` nao implementa Last-Event-ID).
 *
 * Usa `credentials: same-origin` implicito do EventSource — a sessao `agf_session`
 * em cookie passa sem config extra.
 */

export interface SSEClientOptions<T> {
  /** URL absoluta ou relativa do endpoint SSE. */
  url: string;
  /** Handler pra cada evento recebido. Payload eh JSON ja parseado. */
  onMessage: (data: T) => void;
  /** Disparado em cada (re)conexao bem sucedida. Use pra re-hidratar estado. */
  onOpen?: () => void;
  /** Disparado em erro de conexao (o EventSource em si auto-reconecta). */
  onError?: (ev: Event) => void;
}

export interface SSEClient {
  /** Fecha a conexao; idempotente. */
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
 * Coalesce de refreshes: se um refresh esta em voo, marca trailing pra rodar
 * mais um logo depois. Evita perder o "ultimo" evento num burst (problema do
 * `inFlight` simples como lock) e ao mesmo tempo nao dispara N paralelos.
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
        // Trailing tick com pequeno delay pra agrupar eventos proximos.
        setTimeout(run, 50);
      }
    }
  };
  return run;
}
