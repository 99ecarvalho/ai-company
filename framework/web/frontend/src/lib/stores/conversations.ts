import { derived, get, writable } from 'svelte/store';
import {
  listConversations,
  type ConversationSummary,
  type ConvFilter
} from '$lib/api';
import { createSSEClient, coalesceRefresh, type SSEClient } from '$lib/sse';
import { activeConvId, logEvent } from './ui';

/**
 * D-57: lista unificada de threads (mata Mine/Background/Tasks).
 * Um store só, dois filtros de visão (`convFilter`):
 *   - 'active'  -> archived_at IS NULL (default)
 *   - 'closed'  -> archived_at IS NOT NULL (thread soft-deletada manualmente)
 *
 * Distinção "participei vs não participei" passa a ser visual (campo
 * `participating` em cada item), não mais abas separadas.
 *
 * Hierarquia: cada conv carrega `parent_conv_id` (db_id do pai, ou null) e
 * `children_stats` agregado recursivamente. `conversationTree` deriva a
 * arvore pra renderizar na sidebar.
 */
export const conversations = writable<ConversationSummary[]>([]);
export const convFilter = writable<ConvFilter>('active');

/** Query de filtro local da lista de conversas (ConvListPane). Aplicado
 *  sobre topic, agent e conteudo do ultimo preview. Case-insensitive. */
export const convQuery = writable<string>('');

/** Lista filtrada localmente pela query. Compoe com `convFilter` (Active/
 *  Closed) ja aplicado server-side na hora do fetch. */
export const filteredConversations = derived(
  [conversations, convQuery],
  ([items, q]) => {
    const needle = q.trim().toLowerCase();
    if (!needle) return items;
    return items.filter((c) => {
      const topic = (c.topic ?? '').toLowerCase();
      const agent = (c.agent ?? '').toLowerCase();
      const preview = (c.last_msg?.content_preview ?? '').toLowerCase();
      return topic.includes(needle) || agent.includes(needle) || preview.includes(needle);
    });
  }
);

export interface ConvTreeNode {
  conv: ConversationSummary;
  children: ConvTreeNode[];
  /** All direct children considered resolved (ask answered or task terminal).
   *  Kept separate so the sidebar can render them as a collapsed counter. */
  resolvedChildren: ConvTreeNode[];
}

export interface ConvTree {
  /** Top-level nodes (parent_conv_id == null). */
  roots: ConvTreeNode[];
  /** Lookup table by db_id for navigation helpers. */
  byId: Map<number, ConvTreeNode>;
}

/** D-96: determina se uma filha eh "resolvida" do ponto de vista do bucket
 *  de chips (active vs done) no header da raiz.
 *  - awaiting_human / is_running / is_stuck → ativa
 *  - task em status terminal → resolvida
 *  - task_not_current_agent (D-79) → fase concluida, resolvida
 *  - filha sem nada acima → ja terminou seu turno, resolvida
 *  Roots (parent_conv_id null) nao sao classificadas aqui. */
function isResolvedSelf(c: ConversationSummary): boolean {
  if (c.awaiting_human) return false;
  if (c.is_running || c.is_stuck) return false;
  if (c.task && ['done', 'halt', 'human_review'].includes(c.task.status)) return true;
  if (c.task_not_current_agent) return true;
  if (c.parent_conv_id != null) return true;
  return false;
}

/** Tree derivado — aplica `filteredConversations` (busca local) + monta
 *  relacao pai-filha via `parent_conv_id`. Quando ha query de busca, retorna
 *  flat (busca rasa funciona melhor). */
export const conversationTree = derived(
  [conversations, convQuery],
  ([items, q]): ConvTree => {
    const byId = new Map<number, ConvTreeNode>();
    for (const conv of items) {
      byId.set(conv.db_id, { conv, children: [], resolvedChildren: [] });
    }
    const roots: ConvTreeNode[] = [];
    for (const node of byId.values()) {
      const pid = node.conv.parent_conv_id;
      if (pid != null && byId.has(pid)) {
        const parent = byId.get(pid)!;
        if (isResolvedSelf(node.conv)) parent.resolvedChildren.push(node);
        else parent.children.push(node);
      } else {
        roots.push(node);
      }
    }

    // Se tem busca ativa, reduz a arvore: mostra so convs que matcham (flat
    // como root). Filhas matchadas viram root — mais util que esconder sob
    // pais que nao matcham.
    const needle = q.trim().toLowerCase();
    if (needle) {
      const flat: ConvTreeNode[] = [];
      for (const node of byId.values()) {
        const c = node.conv;
        const hay = [
          c.topic ?? '',
          c.agent ?? '',
          c.last_msg?.content_preview ?? ''
        ]
          .join(' ')
          .toLowerCase();
        if (hay.includes(needle)) {
          flat.push({ conv: c, children: [], resolvedChildren: [] } as ConvTreeNode);
        }
      }
      return { roots: flat, byId };
    }

    return { roots, byId };
  }
);

/** Derived helper — pending conv (waiting on human) if any. */
export const pendingConversation = derived(conversations, (items) =>
  items.find((c) => c.awaiting_human) || null
);

// D-96 cleanup: stores `expandedChildren`/`expandedResolved` removidos.
// Pos-flat sidebar nao tem mais tree expand/collapse — so renderiza
// raizes e filhas viraram chips no header (overlay read-only).

let sseClient: SSEClient | null = null;
let inFlight = false;

/** Dedupe defensivo por db_id: o backend pode estar retornando linhas
 *  duplicadas em alguns casos (JOIN sem GROUP BY); Svelte 5 quebra o
 *  {#each} silenciosamente quando ha keys duplicadas. Mantem a ultima
 *  ocorrencia (mais fresca em geral). */
function dedupeByDbId(items: ConversationSummary[]): ConversationSummary[] {
  const map = new Map<number, ConversationSummary>();
  for (const c of items) map.set(c.db_id, c);
  return Array.from(map.values());
}

// D-96: pruneExpansionSets/autoExpandPending removidos — sidebar virou flat
// pos-D-96, sem tree expand/collapse, sem badges cascateadas de descendentes.

export async function refreshConversations() {
  if (inFlight) return;
  inFlight = true;
  try {
    const { items } = await listConversations(get(convFilter));
    const deduped = dedupeByDbId(items);
    conversations.set(deduped);
  } catch (e) {
    logEvent(`refresh: ${e}`, 'err');
  } finally {
    inFlight = false;
  }
}

/**
 * SSE-triggered refresh: o endpoint /api/events emite um evento por mensagem
 * nova (qualquer stream). Cada evento dispara um re-fetch da lista — como
 * mudancas em has_pending_ask/last_msg/children_stats sao server-derived,
 * re-fetch eh autoritativo e simples. Coalesce evita N paralelos num burst.
 */
const refreshTriggered = coalesceRefresh(refreshConversations);

interface MsgEvent {
  id: number;
  conversation_id: number;
  sender_id: number;
  stream?: string;
  topic?: string;
  content?: string;
}

export function startConversationsStream() {
  if (sseClient) return;
  // Hidratacao inicial sincrona (nao bloqueia start do SSE).
  refreshConversations();
  sseClient = createSSEClient<MsgEvent>({
    url: '/api/events',
    onMessage: () => refreshTriggered(),
    // Reconnect: re-hidratar pra cobrir eventos perdidos durante downtime.
    onOpen: () => refreshTriggered()
  });
}

export function stopConversationsStream() {
  if (sseClient) {
    sseClient.close();
    sseClient = null;
  }
}

/** Ao trocar filtro, refresh imediato (active <-> closed vem do backend). */
convFilter.subscribe(() => {
  if (sseClient) refreshConversations();
});

/** Look up a summary by conv id (stream/topic). */
export function findConversation(id: string): ConversationSummary | undefined {
  return get(conversations).find((c) => c.id === id);
}

/** Look up by db_id (used when navigating from deepest_pending_path). */
export function findConversationByDbId(dbId: number): ConversationSummary | undefined {
  return get(conversations).find((c) => c.db_id === dbId);
}

/** Notify watchers of a single removal (close/delete) without waiting for the
 *  next poll tick — improves perceived responsiveness. */
export function dropConversationLocally(id: string) {
  conversations.update((arr) => arr.filter((c) => c.id !== id));
}

/** Otimismo local apos PATCH /api/conversations/{id} — atualiza custom_title
 *  no card sem esperar refresh. Migration 027. */
export function patchConversationLocally(id: string, fields: Partial<ConversationSummary>) {
  conversations.update((arr) => arr.map((c) => (c.id === id ? { ...c, ...fields } : c)));
}

/** Re-export for active conv id derivation. */
export const activeConversation = derived(
  [conversations, activeConvId],
  ([list, id]) => (id ? list.find((c) => c.id === id) || null : null)
);
