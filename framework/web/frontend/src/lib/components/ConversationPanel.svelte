<script lang="ts">
  import { onDestroy, onMount, tick } from 'svelte';
  import {
    ArrowLeft, Send, Paperclip, Archive, ArchiveRestore, Trash2, FileText,
    RefreshCw, Square as SquareIcon, AlertTriangle, Hourglass, Loader2, XCircle
  } from 'lucide-svelte';
  import {
    refreshActiveConversation,
    startMessagesStream,
    stopMessagesStream,
    conversationDetail,
    clearActiveConversation
  } from '$lib/stores/messages';
  import {
    activeConvId,
    logEvent,
    status,
    showCapturePanel,
    showListOnMobile,
    openFileBrowserAt,
    openChildConv,
    showConvPanel
  } from '$lib/stores/ui';
  import { sending, tryBeginSend, endSend } from '$lib/stores/sending';
  import {
    deleteConversation as apiDelete,
    archiveConversation,
    unarchiveConversation,
    getWorkflow,
    getTaskStats,
    postMessage,
    uploadFile,
    cancelConversation,
    retryConversation,
    type WorkflowDef,
    type TaskStats,
    type RunnerStateInfo
  } from '$lib/api';
  import {
    refreshConversations,
    dropConversationLocally,
    activeConversation,
    findConversationByDbId,
    conversationTree
  } from '$lib/stores/conversations';
  import { markSeen } from '$lib/stores/seen';
  import {
    replyAttachments,
    addAttachment,
    removeAttachment,
    clearAttachments,
    attachmentsToMarkdown
  } from '$lib/stores/attachments';
  import { pasteImages } from '$lib/services/paste';
  import { dragDropFiles } from '$lib/services/dragdrop';
  import MessageBubble from './MessageBubble.svelte';
  import AudioRecorder from './AudioRecorder.svelte';
  import AttachmentList from './AttachmentList.svelte';
  import LiveEventLine from './LiveEventLine.svelte';
  import LiveEventGroup from './LiveEventGroup.svelte';
  import ThinkingBubble from './ThinkingBubble.svelte';
  import TodoStatusBar from './TodoStatusBar.svelte';
  import MentionPicker from './MentionPicker.svelte';
  import { fmtCost, fmtMs, fmtDateSeparator, sameDay } from '$lib/services/format';
  import { buildFeed, type FeedItem } from '$lib/services/feed';
  import { closeLiveTrace, liveEvents, openLiveTrace } from '$lib/stores/liveTrace';
  import { pendingSends, markSent, clearPending } from '$lib/stores/pendingSends';
  import type { AudioCtrl } from '$lib/services/audio';

  let text = $state('');
  let textareaEl: HTMLTextAreaElement | null = $state(null);
  let listEl: HTMLDivElement | null = $state(null);
  let fileInputEl: HTMLInputElement | null = $state(null);
  let audioCtrl: AudioCtrl | null = null;

  // Refresh whenever activeConvId changes.
  $effect(() => {
    const id = $activeConvId;
    stickToBottom = true; // a new conv always starts pinned to the bottom
    if (id) {
      refreshActiveConversation();
      openLiveTrace(id);
    } else {
      closeLiveTrace();
    }
  });

  // Smart auto-scroll: captures whether the user was "at the bottom" BEFORE
  // the update. If so, scrolls back to the bottom afterwards. If scrolled up
  // (reading history), preserves the position. Tight threshold (4px) — covers
  // browser sub-pixel rounding without letting a "small scroll up" keep it
  // stuck.
  const SCROLL_BOTTOM_THRESHOLD_PX = 4;

  function isAtBottom(el: HTMLDivElement): boolean {
    return el.scrollHeight - el.scrollTop - el.clientHeight <= SCROLL_BOTTOM_THRESHOLD_PX;
  }

  let stickToBottom = $state(true); // default true: a new conv opens at the bottom
  // Flag to ignore scroll events caused by the programmatic auto-scroll.
  // Without it, auto-scroll fired onscroll -> isAtBottom=true -> stick
  // got stuck at true even when the intent was to preserve the position.
  let programmaticScroll = false;

  function onListScroll() {
    if (programmaticScroll) return;
    if (listEl) stickToBottom = isAtBottom(listEl);
  }

  $effect(() => {
    // Reacts to changes in messages OR live events. If stickToBottom,
    // auto-scrolls; otherwise preserves the position the user scrolled to.
    void $conversationDetail;
    void $liveEvents;
    if (listEl && stickToBottom) {
      tick().then(() => {
        if (listEl && stickToBottom) {
          programmaticScroll = true;
          listEl.scrollTop = listEl.scrollHeight;
          // Clear the flag after the synthetic scroll event is processed.
          requestAnimationFrame(() => {
            requestAnimationFrame(() => { programmaticScroll = false; });
          });
        }
      });
    }
  });

  // Marks the active conv as "read" up to its last_activity. Fires on entering
  // the conv (initial effect) and every time polling brings a newer
  // last_activity — so a new message while the user is watching isn't
  // marked as unread.
  $effect(() => {
    const active = $activeConversation;
    if (active) markSeen(active.id, active.last_activity);
  });

  // D-71: invalidates runner_state when run_start/run_end arrives via SSE —
  // so the badge and the Retry/Cancel buttons appear/disappear without waiting
  // for the next polling cycle (REFRESH_MS). Idempotent: refresh is the same
  // /messages fetch.
  let lastSeenRunEventIdx = $state(-1);
  $effect(() => {
    const events = $liveEvents;
    for (let i = events.length - 1; i > lastSeenRunEventIdx; i--) {
      const kind = events[i]?.kind;
      if (kind === 'run_start' || kind === 'run_end') {
        lastSeenRunEventIdx = events.length - 1;
        void refreshActiveConversation();
        break;
      }
    }
    if (events.length - 1 > lastSeenRunEventIdx) {
      lastSeenRunEventIdx = events.length - 1;
    }
  });

  onMount(() => {
    startMessagesStream();
    if (window.matchMedia?.('(hover: hover) and (pointer: fine)').matches) {
      tick().then(() => textareaEl?.focus());
    }
  });

  onDestroy(() => {
    stopMessagesStream();
    clearActiveConversation();
    closeLiveTrace();
  });

  async function send() {
    const id = $activeConvId;
    if (!id) return;
    if (!tryBeginSend()) return;
    status.set('sending…');
    try {
      const [stream, ...rest] = id.split('/');
      const topic = rest.join('/');
      const fullContent = text.trim() + attachmentsToMarkdown($replyAttachments);
      await postMessage({ stream, topic, content: fullContent });
      logEvent(`sent → ${topic}`, 'ok');
      markSent(id);
      text = '';
      clearAttachments('reply');
      status.set('ready');
      setTimeout(() => {
        refreshActiveConversation();
        refreshConversations();
      }, 500);
    } catch (e) {
      logEvent(`send: ${e}`, 'err');
      status.set('send failed');
    } finally {
      endSend();
    }
  }

  async function archive() {
    const id = $activeConvId;
    if (!id) return;
    const descN = descendantCount;
    if (
      descN > 0 &&
      !confirm(
        `Archive this thread and ${descN} sub-conversation${descN === 1 ? '' : 's'} (cascade)?`
      )
    ) {
      return;
    }
    try {
      const res = await archiveConversation(id);
      const n = res.cascaded_ids?.length ?? 0;
      logEvent(n > 0 ? `archived (+${n} cascaded)` : 'archived', 'ok');
      dropConversationLocally(id);
      refreshConversations();
      showCapturePanel();
    } catch (e) {
      logEvent(`archive: ${e}`, 'err');
    }
  }

  async function unarchive() {
    const id = $activeConvId;
    if (!id) return;
    try {
      const res = await unarchiveConversation(id);
      const n = res.cascaded_ids?.length ?? 0;
      logEvent(n > 0 ? `unarchived (+${n} cascaded)` : 'unarchived', 'ok');
      refreshConversations();
    } catch (e) {
      logEvent(`unarchive: ${e}`, 'err');
    }
  }

  // D-71: re-enqueue the last trigger message so the agent reprocesses it.
  // Backend guards with can_retry (errored/stuck only); double-click → 409.
  async function retryRun() {
    const id = $activeConvId;
    if (!id) return;
    if (!confirm('Retry the last turn? The agent will re-process the message that originated the current turn.')) return;
    retryInFlight = true;
    try {
      await retryConversation(id);
      logEvent('retry dispatched', 'ok');
      // SSE run_start will invalidate runner_state; immediate backup:
      refreshActiveConversation();
    } catch (e) {
      logEvent(`retry: ${e}`, 'err');
      retryInFlight = false;
    }
  }

  let retryInFlight = $state(false);
  $effect(() => {
    if (runnerState && !runnerState.can_retry) retryInFlight = false;
  });

  // D-71: cancel via agent_ctrl channel. If Claude CLI is running,
  // dispatcher sends SIGTERM (+SIGKILL fallback after 3s).
  async function cancelRun() {
    const id = $activeConvId;
    if (!id) return;
    if (!confirm('Cancel the current run? If Claude CLI is processing, it will receive SIGTERM.')) return;
    cancelInFlight = true; // hide Cancel button optimistically
    try {
      await cancelConversation(id);
      logEvent('cancel dispatched', 'ok');
      // Runner emits synthetic run_end when the CLI is killed without a
      // `type=result` — SSE will invalidate runner_state. Backup refetch
      // covers cases where the run_end lands before we re-subscribe.
      setTimeout(() => void refreshActiveConversation(), 500);
    } catch (e) {
      logEvent(`cancel: ${e}`, 'err');
      cancelInFlight = false;
    }
  }

  // D-71: optimistic flag to hide Cancel as soon as the user clicks it —
  // avoids the flicker where SIGTERM is in-flight but run_end hasn't
  // landed yet. Reset when runner_state actually transitions out of a
  // cancelable state (SSE or polling).
  let cancelInFlight = $state(false);
  $effect(() => {
    if (runnerState && !runnerState.can_cancel) cancelInFlight = false;
  });

  async function deleteForever() {
    const id = $activeConvId;
    if (!id) return;
    const topic = summary?.topic || id;
    const descN = descendantCount;
    const warning =
      descN > 0
        ? `Delete "${topic}" AND ${descN} sub-conversation${descN === 1 ? '' : 's'} forever? All messages will be lost.`
        : `Delete "${topic}" forever? All messages will be lost.`;
    if (!confirm(warning)) return;
    try {
      const res = await apiDelete(id);
      const n = res.cascaded_ids?.length ?? 0;
      logEvent(n > 0 ? `deleted ${id} (+${n} cascaded)` : `deleted ${id}`, 'ok');
      dropConversationLocally(id);
      refreshConversations();
      showCapturePanel();
    } catch (e) {
      logEvent(`delete: ${e}`, 'err');
    }
  }

  function openTaskArtifacts() {
    const slug = summary?.task?.slug;
    if (!slug) return;
    // Artifacts = the .md files produced by the phases. Opens in the FileBrowser
    // overlay (rule: don't take the human out of the conversation). Clicking a
    // .md inside the browser opens the nested FileViewer overlay.
    openFileBrowserAt(`company/tasks/${slug}`);
  }

  async function uploadFromInput(file: File) {
    status.set('uploading…');
    try {
      const info = await uploadFile(file);
      addAttachment('reply', info);
      logEvent(`attached: ${info.path} (${Math.round(info.size / 1024)}KB)`, 'ok');
      status.set('ready');
    } catch (e) {
      logEvent(`upload: ${e}`, 'err');
      status.set('upload failed');
    }
  }

  function onAttachChange(e: Event) {
    const input = e.target as HTMLInputElement;
    const f = input.files && input.files[0];
    if (!f) return;
    uploadFromInput(f).finally(() => {
      input.value = '';
    });
  }

  let mentionPicker = $state<MentionPicker | null>(null);

  function onKeydown(e: KeyboardEvent) {
    // @-mention picker swallows arrows/Enter/Tab/Esc when active.
    if (mentionPicker?.onTextareaKeydown(e)) return;
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
      e.preventDefault();
      send();
    }
  }

  function onInput() {
    mentionPicker?.onTextareaInput();
  }

  function injectTranscribed(t: string) {
    const cur = text.trim();
    text = cur ? `${cur}\n${t}` : t;
    textareaEl?.focus();
  }

  const summary = $derived($activeConversation);
  const detail = $derived($conversationDetail);
  const isArchived = $derived(!!summary?.archived_at);
  const descendantCount = $derived(
    (summary?.children_stats?.active ?? 0) + (summary?.children_stats?.resolved ?? 0)
  );

  // D-71: runner state comes embedded in detail.runner_state (enriched in
  // GET /messages). Ticks every 10s to update "X min ago" without refetching.
  const runnerState = $derived<RunnerStateInfo | null>(detail?.runner_state ?? null);
  let clockNow = $state(Date.now());
  onMount(() => {
    const iv = setInterval(() => (clockNow = Date.now()), 10000);
    return () => clearInterval(iv);
  });
  function formatAgo(iso: string | null, now: number): string {
    if (!iso) return '';
    const t = new Date(iso).getTime();
    const sec = Math.max(0, Math.round((now - t) / 1000));
    if (sec < 60) return `${sec}s`;
    const min = Math.round(sec / 60);
    if (min < 60) return `${min}min`;
    const h = Math.floor(min / 60);
    const m = min % 60;
    return m ? `${h}h${m}` : `${h}h`;
  }
  const runnerStateLabel = $derived(
    runnerState
      ? {
          idle: 'idle',
          running: 'running',
          stuck: 'stuck',
          errored: 'error',
          awaiting_human: 'awaiting human'
        }[runnerState.state] || runnerState.state
      : ''
  );
  const runnerStateColor = $derived(
    runnerState
      ? {
          idle: 'bg-panel2 text-muted',
          running: 'bg-accent/20 text-accent',
          stuck: 'bg-warn/30 text-warn',
          errored: 'bg-accent2/20 text-accent2',
          awaiting_human: 'bg-warn/20 text-warn'
        }[runnerState.state] || 'bg-panel2 text-muted'
      : ''
  );

  // D-57: if the conversation has a task attached, loads the workflow definition
  // to render a dynamic progress bar. Lazy fetch — only when a task with a
  // declared workflow shows up. Invalidated when the workflow changes.
  let workflowDef = $state<WorkflowDef | null>(null);
  let loadedWorkflowName = $state<string | null>(null);

  $effect(() => {
    const wfName = summary?.task?.workflow || null;
    if (!wfName) {
      workflowDef = null;
      loadedWorkflowName = null;
      return;
    }
    if (wfName === loadedWorkflowName) return;
    loadedWorkflowName = wfName;
    getWorkflow(wfName)
      .then((def) => {
        // Race guard: another task loaded in the meantime.
        if (loadedWorkflowName === wfName) workflowDef = def;
      })
      .catch((e) => {
        logEvent(`workflow fetch: ${e}`, 'err');
        workflowDef = null;
      });
  });

  // Aggregated stats for the whole task (sum over all of the task's
  // conversations, not just this thread). Light polling fetch — the
  // /api/tasks/{slug}/stats API returns only 5 numbers, negligible payload.
  let taskStats = $state<TaskStats | null>(null);
  let lastStatsSlug = $state<string | null>(null);
  let statsTimer: ReturnType<typeof setInterval> | null = null;

  async function refreshTaskStats(slug: string) {
    try {
      const s = await getTaskStats(slug);
      if (lastStatsSlug === slug) taskStats = s;
    } catch (e) {
      logEvent(`task stats: ${e}`, 'err');
    }
  }

  $effect(() => {
    const slug = summary?.task?.slug || null;
    if (slug === lastStatsSlug) return;
    lastStatsSlug = slug;
    taskStats = null;
    if (statsTimer) {
      clearInterval(statsTimer);
      statsTimer = null;
    }
    if (slug) {
      refreshTaskStats(slug);
      statsTimer = setInterval(() => refreshTaskStats(slug), 8000);
    }
  });

  onDestroy(() => {
    if (statsTimer) clearInterval(statsTimer);
  });

  type StepProgress = { name: string; state: 'done' | 'current' | 'pending' };
  const progressSteps = $derived.by<StepProgress[]>(() => {
    if (!workflowDef || !summary?.task) return [];
    const current = summary.task.current_step;
    const status = summary.task.status;
    const ordered = workflowDef.steps_ordered;
    // Terminal: everything done.
    if (status === 'done') {
      return ordered.map((n) => ({ name: n, state: 'done' }));
    }
    const idx = current ? ordered.indexOf(current) : -1;
    return ordered.map((n, i) => ({
      name: n,
      state: i < idx ? 'done' : i === idx ? 'current' : 'pending'
    }));
  });

  // Interleaved feed: messages + live events. Logic lives in $lib/services/feed
  // for reuse by ChildConvOverlay (read-only).
  const feed = $derived<FeedItem[]>(buildFeed(detail?.messages ?? [], $liveEvents));

  /** Pending-send indicator: shows an "agent is starting..." skeleton in the gap
   *  between the send and the agent's first signal (thinking, tool_use, is_bot msg).
   *  Clears automatically when new activity is detected. 60s timeout as a
   *  fallback. */
  const pendingTs = $derived($activeConvId ? $pendingSends[$activeConvId] ?? null : null);

  $effect(() => {
    const id = $activeConvId;
    const ts = pendingTs;
    if (!id || !ts) return;
    // Look for bot activity with ts >= pendingTs.
    const hasBotActivity = feed.some((i) => {
      if (i.ts < ts) return false;
      if (
        i.kind === 'thinking' ||
        i.kind === 'tool_use' ||
        i.kind === 'tool_use_group' ||
        i.kind === 'tool_error'
      )
        return true;
      if (i.kind === 'msg' && i.msg.is_bot) return true;
      return false;
    });
    if (hasBotActivity) clearPending(id);
  });

  $effect(() => {
    const id = $activeConvId;
    const ts = pendingTs;
    if (!id || !ts) return;
    const timer = setTimeout(() => clearPending(id), 60_000);
    return () => clearTimeout(timer);
  });

  /** Tool usage stats by name (stripNs) — pills in the conv header.
   *  Counts tool_use events + paired errors (tool_result is_error=true)
   *  to show "12 reads · 4 bash · 1 error". */
  const toolStats = $derived.by(() => {
    const counts = new Map<string, number>();
    let errors = 0;
    const byId = new Map<number, (typeof $liveEvents)[number]>();
    for (const e of $liveEvents) byId.set(e.id, e);
    // Pair tool_use → next same-agent tool_result (mirrors feed.ts heuristic).
    const sorted = [...$liveEvents].sort((a, b) => a.ts - b.ts || a.id - b.id);
    const errIds = new Set<number>();
    for (let i = 0; i < sorted.length; i++) {
      const cur = sorted[i];
      if (cur.kind !== 'tool_use') continue;
      for (let j = i + 1; j < sorted.length; j++) {
        const nx = sorted[j];
        if (nx.agent !== cur.agent) continue;
        if (nx.kind === 'tool_use') break;
        if (nx.kind === 'tool_result') {
          if ((nx.data as { is_error?: boolean } | undefined)?.is_error === true) {
            errIds.add(cur.id);
          }
          break;
        }
      }
    }
    for (const e of sorted) {
      if (e.kind !== 'tool_use') continue;
      const tool = String((e.data as { tool?: unknown } | undefined)?.tool ?? '');
      const bare = tool.includes('__')
        ? tool.split('__').filter(Boolean).pop() || tool
        : tool || '?';
      counts.set(bare, (counts.get(bare) ?? 0) + 1);
      if (errIds.has(e.id)) errors += 1;
    }
    const items = [...counts.entries()]
      .sort((a, b) => b[1] - a[1])
      .map(([name, n]) => ({ name, n }));
    return { items, errors, total: items.reduce((s, i) => s + i.n, 0) };
  });

  /** Global toggle to hide tool_use lines (and groups/errors) from the feed.
   *  Persisted in localStorage; default ON. */
  let showToolCalls = $state(true);
  onMount(() => {
    try {
      const v = localStorage.getItem('convPanel.showToolCalls');
      if (v === '0') showToolCalls = false;
    } catch {
      /* ignore */
    }
  });
  function toggleShowToolCalls() {
    showToolCalls = !showToolCalls;
    try {
      localStorage.setItem('convPanel.showToolCalls', showToolCalls ? '1' : '0');
    } catch {
      /* ignore */
    }
  }

  /** Feed actually rendered: applies the toggle filter. */
  const visibleFeed = $derived<FeedItem[]>(
    showToolCalls
      ? feed
      : feed.filter(
          (it) =>
            it.kind !== 'tool_use' &&
            it.kind !== 'tool_use_group' &&
            it.kind !== 'tool_error',
        ),
  );

  /** Topic aggregation: sums cost/duration/turns of the run_end events
   *  present in $liveEvents (initial fetch via /live/recent + SSE). */
  const topicStats = $derived.by(() => {
    let cost = 0;
    let duration = 0;
    let turns = 0;
    let runs = 0;
    for (const e of $liveEvents) {
      if (e.kind !== 'run_end') continue;
      const d = (e.data ?? {}) as {
        cost_usd?: number | null;
        duration_ms?: number | null;
        num_turns?: number | null;
      };
      runs += 1;
      if (typeof d.cost_usd === 'number') cost += d.cost_usd;
      if (typeof d.duration_ms === 'number') duration += d.duration_ms;
      if (typeof d.num_turns === 'number') turns += d.num_turns;
    }
    return { runs, cost, duration, turns };
  });
  // D-76 + D-93: child conversations (ask_agent reply, task-<slug> child
  // post complete_phase, notify_human terminal) become read-only for the
  // human once the agent finished its run with no open ask. Backend enforces
  // with 409; UI hides the composer + shows a hint linking to the parent so
  // the user isn't greeted by an error only after clicking Send. Both summary
  // (sidebar) and detail (active conv) carry the flag for the case where the
  // user navigates straight to a completed child without the summary loaded.
  // D-96: any child conv (parent_conv_id != null) is read-only for the human.
  // The child talks to the parent via normal messages; the human replies only
  // on the parent. Before (D-93) only "completed" children were read-only; now
  // it's general — multi-nesting goes away, it's clear where to reply.
  const parentConvId = $derived(
    summary?.parent_conv_id ?? detail?.parent_conv_id ?? null
  );
  const isCompletedChild = $derived(parentConvId != null);
  const completedParentConvId = $derived(parentConvId);

  function goToParent(e: MouseEvent) {
    e.preventDefault();
    if (completedParentConvId == null) return;
    const target = findConversationByDbId(completedParentConvId);
    if (target) showConvPanel(target.id);
  }

  // D-96: child chips in the header — the user clicks and navigates to the child
  // conv (shown read-only). Children leave the sidebar; the chip is the only
  // visual entry point. States reflect `is_running`/`is_stuck`/`is_errored`
  // from the summary (flat signals already computed in broker.py).
  const directChildren = $derived.by(() => {
    if (!summary || summary.parent_conv_id != null) return [] as Array<{
      db_id: number; conv_id: string; agent: string;
      running: boolean; stuck: boolean; errored: boolean; awaiting: boolean;
    }>;
    const node = $conversationTree.byId.get(summary.db_id);
    if (!node) return [];
    const all = [...node.children, ...node.resolvedChildren];
    return all.map((n) => ({
      db_id: n.conv.db_id,
      conv_id: n.conv.id,
      agent: n.conv.agent || n.conv.stream || '?',
      running: n.conv.is_running === true,
      stuck: n.conv.is_stuck === true,
      errored: n.conv.is_errored === true,
      awaiting: n.conv.awaiting_human === true,
    }));
  });

  function openChild(e: MouseEvent, convId: string) {
    e.preventDefault();
    openChildConv(convId);
  }

  // D-96 cleanup: removed `waitingOnDescendant` + "Jump to child" — a child
  // has no ask_human (gated in MCP), so `children_stats.awaiting_human`
  // is always 0 on a root. Simpler flow: if there's a pending_ask, it's on the
  // root itself, and the composer is available to reply.

  const canSend = $derived(
    (!!text.trim() || $replyAttachments.length > 0)
      && !!$activeConvId
      && !$sending
      && !isCompletedChild
  );
  const headerTitle = $derived(detail?.agent || summary?.agent || '?');
  // Migration 027: the header reflects the customizable title when present.
  // Fallback: topic. (We don't use task.title here — it already shows up
  // elsewhere in the header for tasks.)
  const headerMeta = $derived(
    detail
      ? `#${detail.stream} · ${detail.custom_title || detail.topic}`
      : summary
        ? `#${summary.stream} · ${summary.custom_title || summary.topic}`
        : ''
  );
</script>

<section
  class="flex h-full flex-col"
  use:dragDropFiles={{
    onUpload: (info) => addAttachment('reply', info),
    onError: (e) => logEvent(`drop upload: ${e}`, 'err'),
    onStart: () => status.set('uploading…'),
    onDone: () => status.set('ready')
  }}
>
  <header class="flex items-center gap-2 border-b border-border bg-panel px-3 py-2 md:px-4 md:py-2.5">
    <button
      type="button"
      onclick={showListOnMobile}
      class="inline-flex min-h-tap min-w-tap items-center justify-center rounded-md text-muted transition-colors hover:bg-panel2 hover:text-fg md:hidden"
      aria-label="Back"
    >
      <ArrowLeft class="h-6 w-6" />
    </button>
    <!-- Header info: agent name + state/runner/stats badges. No
         `truncate` on the flex-wrap container (incompatible with white-space:
         nowrap). Individual badges handle their text. Stats badge only on
         sm+ (on mobile it competes with the buttons on the right and gets cut). -->
    <div class="flex min-w-0 flex-1 flex-wrap items-baseline gap-x-2 gap-y-0.5">
      <strong class="min-w-0 truncate text-sm">{headerTitle}</strong>
      <span class="hidden truncate text-xs text-muted xs:inline">{headerMeta}</span>
      {#if detail?.pending_ask_id}
        <span class="rounded-sm bg-warn/20 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-warn">
          waiting
        </span>
      {/if}
      {#if runnerState && runnerState.state !== 'idle'}
        <span
          class={`inline-flex items-center gap-1 rounded-sm px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider ${runnerStateColor}`}
          title={runnerState.last_error ? `Last error: ${runnerState.last_error}` : runnerState.state}
        >
          {#if runnerState.state === 'running'}
            <Loader2 class="h-3 w-3 animate-spin" />
          {:else if runnerState.state === 'stuck'}
            <Hourglass class="h-3 w-3" />
          {:else if runnerState.state === 'errored'}
            <AlertTriangle class="h-3 w-3" />
          {/if}
          {runnerStateLabel}
          {#if runnerState.since}
            <span class="opacity-70">· {formatAgo(runnerState.since, clockNow)}</span>
          {/if}
        </span>
      {/if}
      {#if taskStats && taskStats.runs > 0}
        <span
          class="hidden rounded-sm bg-panel2 px-1.5 py-0.5 font-mono text-[10px] text-muted sm:inline"
          title={`whole task: ${taskStats.runs} run(s) across ${taskStats.conversations} conversation(s)`}
        >
          {taskStats.runs} runs · {taskStats.turns} turns · {fmtCost(taskStats.cost_usd)} · {fmtMs(taskStats.duration_ms)}
        </span>
      {:else if !summary?.task && topicStats.runs > 0}
        <span
          class="hidden rounded-sm bg-panel2 px-1.5 py-0.5 font-mono text-[10px] text-muted sm:inline"
          title={`${topicStats.runs} run(s) in this conversation (window of the last ~50 live events)`}
        >
          {topicStats.runs} runs · {topicStats.turns} turns · {fmtCost(topicStats.cost)} · {fmtMs(topicStats.duration)}
        </span>
      {/if}
    </div>
    {#if summary?.task}
      <button
        type="button"
        onclick={openTaskArtifacts}
        class="inline-flex min-h-tap min-w-tap items-center justify-center gap-1.5 rounded-md border border-border bg-panel2 px-2 text-xs hover:bg-bg md:px-2.5"
        aria-label="Task artifacts"
        title="Task artifacts"
      >
        <FileText class="h-5 w-5 md:h-4 md:w-4" />
        <span class="hidden md:inline">Artifacts</span>
      </button>
    {/if}
    {#if isArchived}
      <button
        type="button"
        onclick={unarchive}
        class="inline-flex min-h-tap min-w-tap items-center justify-center gap-1.5 rounded-md border border-border bg-panel2 px-2 text-xs hover:bg-bg md:px-2.5"
        aria-label="Unarchive"
        title="Unarchive thread (move back to Active)"
      >
        <ArchiveRestore class="h-5 w-5 md:h-4 md:w-4" />
        <span class="hidden md:inline">Unarchive</span>
      </button>
    {:else}
      <button
        type="button"
        onclick={archive}
        class="inline-flex min-h-tap min-w-tap items-center justify-center gap-1.5 rounded-md border border-border bg-panel2 px-2 text-xs hover:bg-bg md:px-2.5"
        aria-label="Archive"
        title="Archive thread (move to Closed)"
      >
        <Archive class="h-5 w-5 md:h-4 md:w-4" />
        <span class="hidden md:inline">Archive</span>
      </button>
    {/if}
    <button
      type="button"
      onclick={deleteForever}
      class="inline-flex min-h-tap min-w-tap items-center justify-center rounded-md border border-border bg-panel2 px-2 text-xs text-accent2 hover:bg-bg"
      aria-label="Delete forever"
      title="Delete forever"
    >
      <Trash2 class="h-5 w-5 md:h-4 md:w-4" />
    </button>
  </header>

  {#if directChildren.length > 0}
    <!-- D-96: child chips (delegations via ask_agent). Replaces the
         multi-nesting in the sidebar. Click opens the child conv read-only.
         Icon reflects the child agent's state:
           Loader2 spin = running, AlertTriangle = stuck, XCircle = errored,
           Hourglass = awaiting_human (legacy convs pre-D-96), none = idle. -->
    <div class="flex flex-wrap items-center gap-1.5 border-b border-border bg-panel2/30 px-3 py-1.5 md:px-4">
      <span class="text-[10px] uppercase tracking-wider text-muted">Delegated:</span>
      {#each directChildren as ch (ch.db_id)}
        <button
          type="button"
          onclick={(e) => openChild(e, ch.conv_id)}
          class="inline-flex items-center gap-1 rounded-full border bg-panel px-2 py-0.5 text-[11px] hover:bg-bg"
          class:border-accent={ch.running}
          class:border-warn={ch.stuck || ch.awaiting}
          class:border-accent2={ch.errored}
          class:border-border={!ch.running && !ch.stuck && !ch.awaiting && !ch.errored}
          aria-label={`Open child conversation with ${ch.agent}`}
          title={
            ch.running ? 'Running' :
            ch.stuck ? 'Stuck (no activity)' :
            ch.errored ? 'Last run errored' :
            ch.awaiting ? 'Waiting on human (legacy)' :
            'Idle'
          }
        >
          {#if ch.running}
            <Loader2 class="h-3 w-3 animate-spin text-accent" />
          {:else if ch.stuck}
            <AlertTriangle class="h-3 w-3 text-warn" />
          {:else if ch.errored}
            <XCircle class="h-3 w-3 text-accent2" />
          {:else if ch.awaiting}
            <Hourglass class="h-3 w-3 text-warn" />
          {/if}
          <span class="font-mono">{ch.agent}</span>
        </button>
      {/each}
    </div>
  {/if}

  {#if progressSteps.length > 0}
    <!-- D-57: dynamic progress bar. Renders whatever /api/workflows/{name}
         returns (steps_ordered). No hardcoded step names — works for any
         workflow declared in the instance. -->
    <div class="flex items-center gap-2 overflow-x-auto border-b border-border bg-panel2/50 px-4 py-1.5 text-[10px] text-muted">
      {#each progressSteps as step, i (step.name)}
        {#if i > 0}<span class="text-muted/50">›</span>{/if}
        <span
          class="inline-flex items-center gap-1 whitespace-nowrap rounded px-1.5 py-0.5 font-mono"
          class:bg-ok={step.state === 'done'}
          class:bg-accent={step.state === 'current'}
          class:text-on-accent={step.state === 'done' || step.state === 'current'}
          class:font-semibold={step.state === 'current'}
          title={step.state}
        >
          {#if step.state === 'done'}✓{:else if step.state === 'current'}⏳{:else}—{/if}
          {step.name}
        </span>
      {/each}
      {#if summary?.task?.status && summary.task.status !== 'in_progress'}
        <span class="ml-2 rounded bg-panel2 px-1.5 py-0.5 uppercase">
          {summary.task.status}
        </span>
      {/if}
    </div>
  {/if}

  {#if toolStats.total > 0}
    <!-- Per-tool count pills + "show tool calls" toggle (D-103). -->
    <div class="flex items-center gap-1.5 overflow-x-auto border-b border-border bg-panel2/30 px-3 py-1 text-[10px]">
      <button
        type="button"
        onclick={toggleShowToolCalls}
        class="inline-flex shrink-0 items-center gap-1 rounded border border-border px-1.5 py-0.5 font-medium {showToolCalls ? 'bg-panel2 text-fg' : 'bg-bg text-muted'}"
        title={showToolCalls ? 'Hide tool calls from feed' : 'Show tool calls in feed'}
        aria-pressed={showToolCalls}
      >
        {showToolCalls ? '👁' : '🚫'} tools
      </button>
      {#each toolStats.items.slice(0, 6) as it (it.name)}
        <span class="inline-flex shrink-0 items-center gap-1 rounded bg-panel2 px-1.5 py-0.5 font-mono text-muted">
          <span class="text-fg">{it.n}</span>
          <span>{it.name}</span>
        </span>
      {/each}
      {#if toolStats.items.length > 6}
        <span class="shrink-0 text-muted/60">+{toolStats.items.length - 6}</span>
      {/if}
      {#if toolStats.errors > 0}
        <span
          class="inline-flex shrink-0 items-center gap-1 rounded bg-accent2/15 px-1.5 py-0.5 font-mono text-accent2"
          title="{toolStats.errors} tool result(s) with is_error=true"
        >
          <span class="font-semibold">{toolStats.errors}</span>
          <span>error{toolStats.errors === 1 ? '' : 's'}</span>
        </span>
      {/if}
    </div>
  {/if}

  <div bind:this={listEl} onscroll={onListScroll} class="flex flex-1 flex-col gap-2 overflow-y-auto p-3 md:p-4" style="overscroll-behavior: contain;">
    {#if detail}
      {#each visibleFeed as item, i (item.id)}
        {@const showDateSeparator = i === 0 || !sameDay(visibleFeed[i - 1].ts, item.ts)}
        {#if showDateSeparator}
          <div class="flex items-center gap-2 py-1 text-[10px] uppercase tracking-wider text-muted">
            <span class="h-px flex-1 bg-border"></span>
            <span class="rounded-full bg-panel2 px-2 py-0.5">{fmtDateSeparator(item.ts)}</span>
            <span class="h-px flex-1 bg-border"></span>
          </div>
        {/if}
        {#if item.kind === 'msg'}
          <MessageBubble msg={item.msg} />
        {:else if item.kind === 'thinking'}
          <ThinkingBubble event={item.event} agent={headerTitle} />
        {:else if item.kind === 'tool_use'}
          <LiveEventLine event={item.event} result={item.result} {workflowDef} />
        {:else if item.kind === 'tool_use_group'}
          <LiveEventGroup
            events={item.events}
            results={item.results}
            tool={item.tool}
            {workflowDef}
          />
        {:else}
          <LiveEventLine event={item.event} {workflowDef} />
        {/if}
      {/each}
      {#if summary?.is_running}
        <div class="text-xs text-muted italic">
          {detail.agent} is processing<span class="dots">…</span>
        </div>
      {:else if pendingTs}
        <!-- Gap between the user send and the first live event/bot msg.
             Disappears automatically once agent activity arrives. -->
        <div class="flex items-center gap-2 rounded-md border border-dashed border-accent/40 bg-accent/5 px-3 py-2 text-xs text-muted">
          <span class="inline-block h-2 w-2 animate-pulse rounded-full bg-accent"></span>
          <span>Message sent — waiting for agent…</span>
        </div>
      {/if}
    {:else}
      <div class="text-xs text-muted">loading…</div>
    {/if}
  </div>

  <TodoStatusBar events={$liveEvents} />

  {#if isCompletedChild && !isArchived}
    <!-- D-96: any child conversation is read-only — humans reply only on
         the root. Child agents communicate with the root agent via normal
         messages; the root decides whether to escalate to the human. -->
    <footer class="flex items-center gap-2 border-t border-border bg-panel px-3 py-2 text-xs text-muted">
      <span class="inline-block h-2 w-2 rounded-full bg-muted/50"></span>
      <span class="flex-1">
        Read-only — this is a delegated sub-conversation. To reply, jump to the
        parent conversation; the parent agent will pass it through.
      </span>
      {#if completedParentConvId != null}
        <button
          type="button"
          onclick={goToParent}
          class="inline-flex items-center gap-1 rounded border border-border bg-panel2 px-2 py-1 text-[11px] font-semibold uppercase tracking-wider hover:bg-bg"
        >
          Jump to parent →
        </button>
      {/if}
    </footer>
  {/if}
  <footer
    class="flex flex-col gap-2 border-t border-border bg-panel p-3"
    class:hidden={isArchived || isCompletedChild}
  >
    <div class="relative">
      <textarea
        bind:this={textareaEl}
        bind:value={text}
        rows="3"
        class="w-full resize-none rounded-md border border-border bg-panel2 p-3 text-sm leading-relaxed focus:border-accent focus:outline-none"
        placeholder="Reply… (Ctrl/Cmd+Enter to send; @ to mention a file)"
        onkeydown={onKeydown}
        oninput={onInput}
        use:pasteImages={{
          onUpload: (info) => addAttachment('reply', info),
          onError: (e) => logEvent(`paste upload: ${e}`, 'err'),
          onStart: () => status.set('uploading…'),
          onDone: () => status.set('ready')
        }}
      ></textarea>
      <MentionPicker
        bind:this={mentionPicker}
        textarea={textareaEl}
        bind:value={text}
        onUpdate={(v) => (text = v)}
      />
    </div>

    <AttachmentList
      items={$replyAttachments}
      onRemove={(i) => removeAttachment('reply', i)}
    />

    <div class="flex flex-wrap items-center justify-between gap-2">
      <div class="flex items-center gap-2">
        <button
          type="button"
          class="inline-flex min-h-tap items-center gap-2 rounded-md border border-border bg-panel2 px-3 py-2 text-sm hover:bg-bg"
          onclick={() => fileInputEl?.click()}
          aria-label="Attach file"
          title="Attach file"
        >
          <Paperclip class="h-5 w-5" />
          <span class="hidden xs:inline">Attach</span>
        </button>
        <input
          bind:this={fileInputEl}
          type="file"
          class="hidden"
          onchange={onAttachChange}
        />
        <AudioRecorder
          onTranscribed={injectTranscribed}
          bindCtrl={(c) => (audioCtrl = c)}
        />
      </div>

      <div class="flex items-center gap-2">
        {#if runnerState?.can_retry && !retryInFlight}
          <button
            type="button"
            onclick={retryRun}
            class="inline-flex min-h-tap items-center gap-2 rounded-md border border-border bg-panel2 px-3 py-2 text-sm hover:bg-bg"
            aria-label="Retry last turn"
            title="Retry last turn (re-process the message that originated the current turn)"
          >
            <RefreshCw class="h-5 w-5" />
            <span class="hidden xs:inline">Retry</span>
          </button>
        {/if}
        {#if runnerState?.can_cancel && !cancelInFlight}
          <button
            type="button"
            onclick={cancelRun}
            class="inline-flex min-h-tap items-center gap-2 rounded-md border border-border bg-panel2 px-3 py-2 text-sm text-accent2 hover:bg-bg"
            aria-label="Cancel current run"
            title="Cancel current run (SIGTERM to Claude CLI if running)"
          >
            <SquareIcon class="h-5 w-5" />
            <span class="hidden xs:inline">Cancel</span>
          </button>
        {/if}
        <button
          type="button"
          class="inline-flex min-h-tap items-center gap-2 rounded-md bg-accent px-4 py-2 text-sm font-semibold text-on-accent hover:bg-accent/90 disabled:cursor-not-allowed disabled:opacity-50"
          disabled={!canSend}
          onclick={send}
        >
          <Send class="h-5 w-5" />
          Send
        </button>
      </div>
    </div>
  </footer>
</section>
