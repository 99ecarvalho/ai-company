import { writable } from 'svelte/store';

/**
 * Timestamp (segundos Unix) do último send bem-sucedido por convId. Usado
 * pela ConversationPanel pra renderizar um skeleton "agent is starting…"
 * no gap entre o send e a primeira atividade do agente.
 *
 * Unidade em segundos pra casar com `ts` de live events e `timestamp` de
 * mensagens (usados por `fmtClock`).
 *
 * Escrita:
 *   - CapturePanel.send() depois de criar a conv (antes do navigate).
 *   - ConversationPanel.send() depois de postar reply.
 * Leitura + limpeza:
 *   - ConversationPanel observa o feed e limpa quando aparece atividade
 *     do bot (thinking, tool_use, ou mensagem com is_bot=true) com ts >=
 *     pendingTs, ou após 60s como fallback.
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
