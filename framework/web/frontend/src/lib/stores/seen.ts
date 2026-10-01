import { writable } from 'svelte/store';

/**
 * Map convId → unix-epoch (seconds) of the last activity seen by the human.
 * Persisted in localStorage. Used for the subtle "unread" badge in the sidebar
 * (a light complement to the strong has_pending_ask badge).
 */
const KEY = 'agf-seen';

function load(): Record<string, number> {
  if (typeof window === 'undefined') return {};
  try {
    const raw = window.localStorage.getItem(KEY);
    return raw ? JSON.parse(raw) : {};
  } catch {
    return {};
  }
}

function persist(map: Record<string, number>) {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.setItem(KEY, JSON.stringify(map));
  } catch {
    /* quota/disabled — ignore */
  }
}

export const seen = writable<Record<string, number>>(load());

/** Marks the conv as read up to the given timestamp (seconds). */
export function markSeen(id: string, lastActivity: number): void {
  seen.update((m) => {
    if ((m[id] ?? 0) >= lastActivity) return m;
    const next = { ...m, [id]: lastActivity };
    persist(next);
    return next;
  });
}

/** Removes from the map — used when closing a conversation (cleans up garbage). */
export function forgetSeen(id: string): void {
  seen.update((m) => {
    if (!(id in m)) return m;
    const { [id]: _, ...rest } = m;
    persist(rest);
    return rest;
  });
}
