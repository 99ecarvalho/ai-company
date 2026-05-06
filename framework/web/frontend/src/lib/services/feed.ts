/**
 * Pure feed builder — interleaves messages + live events into the ordered
 * list rendered by chat panels. Extracted from ConversationPanel so the
 * read-only ChildConvOverlay can reuse the exact same ordering rules.
 *
 * Pairing rule: tool_use ↔ tool_result without explicit tool_use_id, via
 * temporal heuristic on the same agent. Tool_results paired with a tool_use
 * are hidden from the feed (rendered inside the LiveEventLine OUT panel);
 * orphan tool_results with is_error=true surface as standalone error lines.
 *
 * Tiebreak when ts collide:
 *   1. msg before live_event
 *   2. msg x msg: id asc
 *   3. event x event: seq_num asc (fallback to id)
 */
import type { LiveEvent, Message } from '$lib/api';

export type FeedItem =
  | { kind: 'msg'; id: string; ts: number; msg: Message }
  | { kind: 'thinking'; id: string; ts: number; event: LiveEvent }
  | { kind: 'tool_use'; id: string; ts: number; event: LiveEvent; result: LiveEvent | null }
  | {
      kind: 'tool_use_group';
      id: string;
      ts: number;
      tool: string;
      agent: string;
      events: LiveEvent[];
      results: (LiveEvent | null)[];
    }
  | { kind: 'tool_error'; id: string; ts: number; event: LiveEvent };

const GROUP_THRESHOLD = 3;

function stripNs(tool: string): string {
  if (tool.includes('__')) {
    const parts = tool.split('__').filter(Boolean);
    return parts[parts.length - 1] || tool;
  }
  return tool;
}

/** Coalesce N≥3 consecutive tool_use FeedItems with same tool+agent into a
 *  single tool_use_group. Items with paired error result break the run —
 *  errors stay individually visible (red). */
function coalesceToolRuns(items: FeedItem[]): FeedItem[] {
  const out: FeedItem[] = [];
  let i = 0;
  while (i < items.length) {
    const cur = items[i];
    if (cur.kind !== 'tool_use') {
      out.push(cur);
      i++;
      continue;
    }
    const curIsErr = cur.result?.data?.is_error === true;
    if (curIsErr) {
      out.push(cur);
      i++;
      continue;
    }
    const tool = stripNs(String(cur.event.data?.tool ?? ''));
    const agent = cur.event.agent ?? '';
    let j = i + 1;
    while (j < items.length) {
      const nx = items[j];
      if (nx.kind !== 'tool_use') break;
      const nxIsErr = nx.result?.data?.is_error === true;
      if (nxIsErr) break;
      const nxTool = stripNs(String(nx.event.data?.tool ?? ''));
      const nxAgent = nx.event.agent ?? '';
      if (nxTool !== tool || nxAgent !== agent) break;
      j++;
    }
    const runLen = j - i;
    if (runLen >= GROUP_THRESHOLD) {
      const slice = items.slice(i, j) as Extract<FeedItem, { kind: 'tool_use' }>[];
      out.push({
        kind: 'tool_use_group',
        id: `g${slice[0].id}_${slice[slice.length - 1].id}`,
        ts: slice[0].ts,
        tool,
        agent,
        events: slice.map((s) => s.event),
        results: slice.map((s) => s.result),
      });
      i = j;
    } else {
      out.push(cur);
      i++;
    }
  }
  return out;
}

function eventOrderKey(e: LiveEvent): number {
  return e.seq_num ?? e.id;
}

function extractIdNum(id: string): number {
  const m = /\d+$/.exec(id);
  return m ? Number(m[0]) : 0;
}

export function buildFeed(messages: Message[], events: LiveEvent[]): FeedItem[] {
  const items: FeedItem[] = [];
  for (const m of messages) {
    items.push({ kind: 'msg', id: `m${m.id}`, ts: m.timestamp, msg: m });
  }
  const evts = [...events].sort((a, b) => {
    if (a.ts !== b.ts) return a.ts - b.ts;
    return eventOrderKey(a) - eventOrderKey(b);
  });
  const paired = new Set<number>();
  const resultFor = new Map<number, LiveEvent>();
  for (let i = 0; i < evts.length; i++) {
    const e = evts[i];
    if (e.kind !== 'tool_use') continue;
    for (let j = i + 1; j < evts.length; j++) {
      const cand = evts[j];
      if (cand.agent !== e.agent) continue;
      if (cand.kind === 'tool_use') break;
      if (cand.kind === 'tool_result') {
        resultFor.set(e.id, cand);
        paired.add(cand.id);
        break;
      }
    }
  }
  for (const e of evts) {
    if (e.kind === 'thinking') {
      items.push({ kind: 'thinking', id: `e${e.id}`, ts: e.ts, event: e });
    } else if (e.kind === 'tool_use') {
      items.push({
        kind: 'tool_use',
        id: `e${e.id}`,
        ts: e.ts,
        event: e,
        result: resultFor.get(e.id) ?? null
      });
    } else if (e.kind === 'tool_result') {
      if (paired.has(e.id)) continue;
      const isErr =
        (e.data as { is_error?: boolean } | undefined)?.is_error === true ||
        (!e.data && e.summary === 'error');
      if (isErr) {
        items.push({ kind: 'tool_error', id: `e${e.id}`, ts: e.ts, event: e });
      }
    }
    // run_start/run_end: aggregated in header, never in feed.
  }
  function feedOrderKey(item: FeedItem): number {
    if (item.kind === 'msg') return extractIdNum(item.id);
    if (item.kind === 'tool_use_group') return eventOrderKey(item.events[0]);
    return eventOrderKey(item.event);
  }
  items.sort((a, b) => {
    if (a.ts !== b.ts) return a.ts - b.ts;
    if (a.kind === 'msg' && b.kind !== 'msg') return -1;
    if (b.kind === 'msg' && a.kind !== 'msg') return 1;
    return feedOrderKey(a) - feedOrderKey(b);
  });
  return coalesceToolRuns(items);
}
