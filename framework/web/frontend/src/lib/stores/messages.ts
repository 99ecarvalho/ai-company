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
 * SSE emite eventos de msg de todos os streams; filtramos pelo db_id da conv
 * ativa. Quando bate, re-fetch do detail (eh autoritativo pra runner_state,
 * pending_ask_id, sender_full_name, is_self etc que nao vem no payload SSE).
 */
const refreshTriggered = coalesceRefresh(refreshActiveConversation);

export function startMessagesStream() {
  if (sseClient) return;
  sseClient = createSSEClient<MsgEvent>({
    url: '/api/events',
    onMessage: (ev) => {
      // Filtra por conversation_id da conv ativa. Como activeConvId eh
      // "stream/topic" mas o SSE traz db_id, comparamos via conversationDetail.
      const current = get(conversationDetail);
      if (!current) return;
      // conversationDetail nao carrega o db_id (a interface publica usa id
      // "stream/topic"); mas nesse caso o filtro stream+topic bate no evento
      // enriquecido — broker.py injeta stream+topic antes de emitir.
      const evAny = ev as MsgEvent & { stream?: string; topic?: string };
      if (!evAny.stream || !evAny.topic) return;
      if (`${evAny.stream}/${evAny.topic}` !== current.id) return;
      refreshTriggered();
    },
    // On (re)connect, hidrata — cobre mensagens perdidas durante downtime.
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
