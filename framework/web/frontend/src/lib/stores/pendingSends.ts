import { writable } from 'svelte/store';

/**
 * Timestamp (Unix seconds) of the last successful send per convId. Used
 * by ConversationPanel to render an "agent is starting…" skeleton
 * in the gap between the send and the agent's first activity.
 *
 * Unit is seconds to match live events' `ts` and messages' `timestamp`
 * (used by `fmtClock`).
 *
 * Writes:
 *   - CapturePanel.send() after creating the conv (before navigate).
 *   - ConversationPanel.send() after posting a reply.
 * Reads + cleanup:
 *   - ConversationPanel watches the feed and clears it when bot activity
 *     shows up (thinking, tool_use, or a message with is_bot=true) with ts >=
 *     pendingTs, or after 60s as a fallback.
 */
export const pendingSends = writable<Record<string, number>>({});

export function markSent(convId: string): void {
  pendingSends.update((s) => ({ ...s, [convId]: Date.now() / 1000 }));
}

export function clearPending(convId: string): void {
  pendingSends.update((s) => {
    if (!(convId in s)) return s;
    const next = { ...s };
    delete next[convId];
    return next;
  });
}
