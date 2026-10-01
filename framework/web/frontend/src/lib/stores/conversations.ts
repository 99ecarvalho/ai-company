import { derived, get, writable } from 'svelte/store';
import {
  listConversations,
  type ConversationSummary,
  type ConvFilter
} from '$lib/api';
import { createSSEClient, coalesceRefresh, type SSEClient } from '$lib/sse';
import { activeConvId, logEvent } from './ui';

/**
 * D-57: unified thread list (kills Mine/Background/Tasks).
 * A single store, two view filters (`convFilter`):
 *   - 'active'  -> archived_at IS NULL (default)
 *   - 'closed'  -> archived_at IS NOT NULL (thread manually soft-deleted)
 *
 * The "participated vs did not participate" distinction is now visual (the
 * `participating` field on each item), no longer separate tabs.
 *
 * Hierarchy: each conv carries `parent_conv_id` (parent's db_id, or null) and
 * recursively aggregated `children_stats`. `conversationTree` derives the
 * tree to render in the sidebar.
 */
export const conversations = writable<ConversationSummary[]>([]);
export const convFilter = writable<ConvFilter>('active');

/** Local filter query for the conversation list (ConvListPane). Applied
 *  to topic, agent and the content of the last preview. Case-insensitive. */
export const convQuery = writable<string>('');

/** List filtered locally by the query. Composes with `convFilter` (Active/
 *  Closed), already applied server-side at fetch time. */
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

/** D-96: decides whether a child is "resolved" from the point of view of the
 *  chip bucket (active vs done) in the root's header.
 *  - awaiting_human / is_running / is_stuck → active
 *  - task in a terminal status → resolved
 *  - task_not_current_agent (D-79) → phase finished, resolved
 *  - child with none of the above → already finished its turn, resolved
 *  Roots (parent_conv_id null) are not classified here. */
function isResolvedSelf(c: ConversationSummary): boolean {
  if (c.awaiting_human) return false;
  if (c.is_running || c.is_stuck) return false;
  if (c.task && ['done', 'halt', 'human_review'].includes(c.task.status)) return true;
  if (c.task_not_current_agent) return true;
  if (c.parent_conv_id != null) return true;
  return false;
}

/** Derived tree — applies `filteredConversations` (local search) + builds
 *  the parent-child relation via `parent_conv_id`. When there is a search
 *  query, returns it flat (shallow search works better). */
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

    // If a search is active, collapse the tree: show only matching convs (flat
    // as roots). Matching children become roots — more useful than hiding them
    // under parents that don't match.
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

// D-96 cleanup: stores `expandedChildren`/`expandedResolved` removed.
// The post-flat sidebar no longer has tree expand/collapse — it only renders
// roots, and children became chips in the header (read-only overlay).

let sseClient: SSEClient | null = null;
let inFlight = false;

/** Defensive dedupe by db_id: the backend may return duplicate rows
 *  in some cases (JOIN without GROUP BY); Svelte 5 silently breaks
 *  {#each} when there are duplicate keys. Keeps the last
 *  occurrence (usually the freshest). */
function dedupeByDbId(items: ConversationSummary[]): ConversationSummary[] {
  const map = new Map<number, ConversationSummary>();
  for (const c of items) map.set(c.db_id, c);
  return Array.from(map.values());
}

// D-96: pruneExpansionSets/autoExpandPending removed — the sidebar became flat
// after D-96, with no tree expand/collapse and no badges cascaded from descendants.

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
 * SSE-triggered refresh: the /api/events endpoint emits one event per new
 * message (any stream). Each event triggers a re-fetch of the list — since
 * changes to has_pending_ask/last_msg/children_stats are server-derived,
 * a re-fetch is authoritative and simple. Coalescing avoids N in parallel in a burst.
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
  // Initial hydration, fire-and-forget (doesn't block the SSE start).
  refreshConversations();
  sseClient = createSSEClient<MsgEvent>({
    url: '/api/events',
    onMessage: () => refreshTriggered(),
    // Reconnect: re-hydrate to cover events lost during downtime.
    onOpen: () => refreshTriggered()
  });
}

export function stopConversationsStream() {
  if (sseClient) {
    sseClient.close();
    sseClient = null;
  }
}

/** On filter change, refresh immediately (active <-> closed comes from the backend). */
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

/** Local optimism after PATCH /api/conversations/{id} — updates custom_title
 *  on the card without waiting for a refresh. Migration 027. */
export function patchConversationLocally(id: string, fields: Partial<ConversationSummary>) {
  conversations.update((arr) => arr.map((c) => (c.id === id ? { ...c, ...fields } : c)));
}

/** Re-export for active conv id derivation. */
export const activeConversation = derived(
  [conversations, activeConvId],
  ([list, id]) => (id ? list.find((c) => c.id === id) || null : null)
);
