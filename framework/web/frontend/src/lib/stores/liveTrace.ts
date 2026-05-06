import { writable } from 'svelte/store';
import { liveRecent, liveStreamUrl, type LiveEvent } from '$lib/api';

const MAX_EVENTS = 100;

export const liveEvents = writable<LiveEvent[]>([]);

let currentConvId: string | null = null;
let es: EventSource | null = null;

export async function openLiveTrace(convId: string): Promise<void> {
  if (currentConvId === convId && es) return;
  closeLiveTrace();
  currentConvId = convId;
  liveEvents.set([]);
  try {
    const recent = await liveRecent(convId, 50);
    liveEvents.set(recent.items);
  } catch {
    /* ignore */
  }
  if (typeof EventSource === 'undefined') return;
  // Same-origin SSE; cookies enviados automaticamente.
  es = new EventSource(liveStreamUrl(convId));
  es.onmessage = (msg) => {
    try {
      const data = JSON.parse(msg.data) as LiveEvent;
      liveEvents.update((arr) => {
        const next = [...arr, data];
        if (next.length > MAX_EVENTS) next.splice(0, next.length - MAX_EVENTS);
        return next;
      });
    } catch {
      /* ignore malformed payload */
    }
  };
  es.onerror = () => {
    /* EventSource auto-reconnect; no-op */
  };
}

export function closeLiveTrace(): void {
  if (es) {
    es.close();
    es = null;
  }
  currentConvId = null;
  liveEvents.set([]);
}
