import { writable, get } from 'svelte/store';
import { getConversationMessages, type ConversationDetail } from '$lib/api';
import { createSSEClient, coalesceRefresh, type SSEClient } from '$lib/sse';
import { activeConvId, logEvent } from './ui';

export const conversationDetail = writable<ConversationDetail | null>(null);

let inFlight = false;
let sseClient: SSEClient | null = null;

export async function refreshActiveConversation() {
  const id = get(activeConvId);
  if (!id || inFlight) return;
  inFlight = true;
  try {
    const data = await getConversationMessages(id);
    // Guard: while we awaited, the active conv might have changed.
    if (get(activeConvId) === id) {
      conversationDetail.set(data);
    }
  } catch (e) {
    logEvent(`conv: ${e}`, 'err');
  } finally {
    inFlight = false;
  }
}

interface MsgEvent {
  id: number;
  conversation_id: number;
  sender_id: number;
}

/**
 * SSE emits msg events from all streams; we filter by the active conv's
 * db_id. On a match, re-fetch the detail (it is authoritative for runner_state,
 * pending_ask_id, sender_full_name, is_self etc, which aren't in the SSE payload).
 */
const refreshTriggered = coalesceRefresh(refreshActiveConversation);

export function startMessagesStream() {
  if (sseClient) return;
  sseClient = createSSEClient<MsgEvent>({
    url: '/api/events',
    onMessage: (ev) => {
      // Filter by the active conv's conversation_id. Since activeConvId is
      // "stream/topic" but SSE carries db_id, we compare via conversationDetail.
      const current = get(conversationDetail);
      if (!current) return;
      // conversationDetail doesn't carry the db_id (the public interface uses id
      // "stream/topic"); but here the stream+topic filter matches the enriched
      // event — broker.py injects stream+topic before emitting.
      const evAny = ev as MsgEvent & { stream?: string; topic?: string };
      if (!evAny.stream || !evAny.topic) return;
      if (`${evAny.stream}/${evAny.topic}` !== current.id) return;
      refreshTriggered();
    },
    // On (re)connect, hydrate — covers messages lost during downtime.
    onOpen: () => {
      if (get(activeConvId)) refreshTriggered();
    }
  });
}

export function stopMessagesStream() {
  if (sseClient) {
    sseClient.close();
    sseClient = null;
  }
}

export function clearActiveConversation() {
  conversationDetail.set(null);
}
