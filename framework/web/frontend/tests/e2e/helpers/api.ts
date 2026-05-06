/**
 * Helpers HTTP pra preparar/limpar estado durante testes E2E.
 * Tudo same-origin via baseURL — assume `WEB_AUTH_DEV_BYPASS=1`.
 */
import { type APIRequestContext, expect } from '@playwright/test';

export interface PostMessageResult {
  id: number;
  stream: string;
  topic: string;
  conversation_id: number;
  content: string;
  sent_at: string;
}

export async function postMessage(
  request: APIRequestContext,
  stream: string,
  topic: string | null,
  content: string
): Promise<PostMessageResult> {
  const r = await request.post('/api/post-message', {
    data: { stream, topic, content }
  });
  expect(r.ok(), await r.text()).toBeTruthy();
  return r.json();
}

export async function closeConversation(
  request: APIRequestContext,
  convId: string
): Promise<void> {
  const enc = convId.includes('/')
    ? `${encodeURIComponent(convId.split('/')[0])}/${encodeURIComponent(convId.split('/').slice(1).join('/'))}`
    : encodeURIComponent(convId);
  await request.post(`/api/conversations/${enc}/close`);
}

export interface Message {
  id: number;
  sender: string;
  is_bot: boolean;
  content: string;
  timestamp: number;
}

export async function getMessages(
  request: APIRequestContext,
  convId: string
): Promise<{ messages: Message[]; agent: string }> {
  const enc = convId.includes('/')
    ? `${encodeURIComponent(convId.split('/')[0])}/${encodeURIComponent(convId.split('/').slice(1).join('/'))}`
    : encodeURIComponent(convId);
  const r = await request.get(`/api/conversations/${enc}/messages`);
  expect(r.ok()).toBeTruthy();
  return r.json();
}

/**
 * Aguarda o agente responder (msg de bot real, ignorando acks/queue
 * notifications). Polling de 500ms ate timeout. Pre-condicao: agente
 * roda com CLAUDE_MOCK=1 + POOL_SIZE/IDLE_TIMEOUT_SEC adequados pra
 * nao acumular workers (test-e2e exporta POOL_SIZE=10 IDLE_TIMEOUT_SEC=15).
 */
function isAckOrQueueMsg(content: string): boolean {
  return (
    content.startsWith(':hourglass') ||
    content.includes('entrou na fila') ||
    content.includes('Ocupado;')
  );
}

export async function waitForBotReply(
  request: APIRequestContext,
  convId: string,
  timeoutMs = 20_000
): Promise<Message> {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    const { messages } = await getMessages(request, convId);
    const reply = messages.find(
      (m) => m.is_bot && !isAckOrQueueMsg(m.content)
    );
    if (reply) return reply;
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error(
    `Timeout waiting for bot reply em ${convId} (${timeoutMs}ms). ` +
    `Stack precisa CLAUDE_MOCK=1 + POOL_SIZE/IDLE_TIMEOUT_SEC adequados.`
  );
}

/** Faz o seed completo: posta msg + aguarda reply do bot mock. */
export async function seedRun(
  request: APIRequestContext,
  stream: string,
  topic: string,
  content: string
): Promise<{ convId: string; reply: Message }> {
  const sent = await postMessage(request, stream, topic, content);
  const convId = `${sent.stream}/${sent.topic}`;
  const reply = await waitForBotReply(request, convId);
  return { convId, reply };
}

export async function closeAll(request: APIRequestContext): Promise<void> {
  await request.post('/api/conversations/close-all');
}

export async function listStreams(
  request: APIRequestContext
): Promise<{ streams: { id: number; name: string }[]; default: string }> {
  const r = await request.get('/api/streams');
  expect(r.ok()).toBeTruthy();
  return r.json();
}

/** Retorna true se ha algum agente capaz de rodar com mock (basta
 * existir streams). */
export async function hasAnyAgent(request: APIRequestContext): Promise<boolean> {
  const { streams } = await listStreams(request);
  return streams.length > 0;
}

export async function deleteAgentPolicyByName(
  request: APIRequestContext,
  agent: string
): Promise<void> {
  await request.delete(`/api/agent-policies/${encodeURIComponent(agent)}`);
}
