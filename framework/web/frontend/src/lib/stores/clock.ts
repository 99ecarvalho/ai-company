import { readable } from 'svelte/store';

/**
 * Reactive clock — ticks every 1s, emits Date.now() in ms.
 *
 * Used by components that render "Xs ago" / "19s ago" without refetching:
 * instead of polling that re-downloaded the list just to re-render, we keep the
 * data in memory (SSE-triggered) and only re-evaluate the formatter when
 * `$now` changes.
 *
 * Usage pattern:
 *   ```
 *   import { now } from '$lib/stores/clock';
 *   import { fmtAge } from '$lib/services/format';
 *   ...
 *   <span>{fmtAge(conv.last_activity, $now)}</span>
 *   ```
 *
 * The readable store only runs setInterval while it has an active subscriber
 * (lazy) and clears it when the last one unsubscribes — zero cost on routes
 * that don't use it.
 */
export const now = readable<number>(Date.now(), (set) => {
  set(Date.now());
  const id = setInterval(() => set(Date.now()), 1000);
  return () => clearInterval(id);
});
