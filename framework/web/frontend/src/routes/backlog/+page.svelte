<script lang="ts">
  import { onMount } from 'svelte';
  import { Plus, Trash2, Play, Pencil, X, ArrowUp, ArrowDown, Undo2, RotateCcw, Bomb } from 'lucide-svelte';
  import PageHeader from '$lib/components/ui/PageHeader.svelte';
  import BacklogFormModal from '$lib/components/backlog/BacklogFormModal.svelte';
  import PromoteTaskModal from '$lib/components/backlog/PromoteTaskModal.svelte';
  import {
    listBacklog,
    createBacklog,
    patchBacklog,
    deleteBacklog,
    promoteBacklog,
    revertBacklog,
    reopenBacklog,
    forceResetBacklog,
    listStreams,
    type BacklogItem,
    type StreamInfo,
    type BacklogCreateInput,
    type BacklogPromoteInput
  } from '$lib/api';
  import { logEvent } from '$lib/stores/ui';

  type GroupBy = 'priority' | 'impact' | 'effort';

  let items: BacklogItem[] = $state([]);
  let streams: StreamInfo[] = $state([]);
  let statusFilter = $state<'aberto' | 'rascunho' | 'em_execucao' | 'promovido' | 'concluido' | 'descartado' | 'all'>('aberto');
  let groupBy = $state<GroupBy>('priority');
  let loading = $state(false);
  let error = $state<string | null>(null);

  // Modal state
  let createOpen = $state(false);
  let editingItem = $state<BacklogItem | null>(null);
  let promotingItem = $state<BacklogItem | null>(null);

  const IMPACT_RANK: Record<string, number> = { alto: 3, medio: 2, baixo: 1 };
  const EFFORT_RANK: Record<string, number> = { baixo: 1, medio: 2, alto: 3 };

  // Column definitions per group-by mode. Order = left→right in the board.
  const PRIORITY_COLUMNS: Array<{ key: string; label: string; match: (it: BacklogItem) => boolean }> = [
    { key: '2',  label: 'critical',  match: (it) => it.priority >= 2 },
    { key: '1',  label: 'high',      match: (it) => it.priority === 1 },
    { key: '0',  label: 'normal',    match: (it) => it.priority === 0 },
    { key: '-1', label: 'low',       match: (it) => it.priority === -1 },
    { key: '-2', label: 'very-low',  match: (it) => it.priority <= -2 }
  ];
  const IMPACT_COLUMNS: Array<{ key: string; label: string; match: (it: BacklogItem) => boolean }> = [
    { key: 'alto',  label: 'high',   match: (it) => it.impact === 'alto' },
    { key: 'medio', label: 'medium', match: (it) => it.impact === 'medio' },
    { key: 'baixo', label: 'low',    match: (it) => it.impact === 'baixo' },
    { key: 'none',  label: '—',      match: (it) => !it.impact }
  ];
  const EFFORT_COLUMNS: Array<{ key: string; label: string; match: (it: BacklogItem) => boolean }> = [
    { key: 'baixo', label: 'low',    match: (it) => it.effort === 'baixo' },
    { key: 'medio', label: 'medium', match: (it) => it.effort === 'medio' },
    { key: 'alto',  label: 'high',   match: (it) => it.effort === 'alto' },
    { key: 'none',  label: '—',      match: (it) => !it.effort }
  ];

  function compareForMode(mode: GroupBy, a: BacklogItem, b: BacklogItem): number {
    const pri = (x: BacklogItem) => x.priority;
    const imp = (x: BacklogItem) => IMPACT_RANK[x.impact ?? ''] ?? 0;
    const eff = (x: BacklogItem) => EFFORT_RANK[x.effort ?? ''] ?? 99; // missing = last
    if (mode === 'impact') {
      // priority DESC, then effort ASC (low effort first = quick win)
      return (pri(b) - pri(a)) || (eff(a) - eff(b));
    }
    if (mode === 'effort') {
      // priority DESC, then impact DESC
      return (pri(b) - pri(a)) || (imp(b) - imp(a));
    }
    // priority mode: impact DESC, then effort ASC
    return (imp(b) - imp(a)) || (eff(a) - eff(b));
  }

  const columns = $derived.by(() => {
    const defs =
      groupBy === 'priority' ? PRIORITY_COLUMNS
      : groupBy === 'impact' ? IMPACT_COLUMNS
      : EFFORT_COLUMNS;
    const out = defs.map((c) => {
      const bucket = items.filter(c.match);
      bucket.sort((a, b) => compareForMode(groupBy, a, b));
      return { ...c, items: bucket };
    });
    // Hide the "—" null column unless it has items (for impact/effort).
    return out.filter((c) => c.key !== 'none' || c.items.length > 0);
  });

  async function load() {
    loading = true;
    error = null;
    try {
      if (statusFilter === 'all') {
        const r = await listBacklog(undefined, true);
        items = r.items;
      } else {
        const r = await listBacklog(statusFilter);
        items = r.items;
      }
    } catch (e) {
      error = String(e);
    } finally {
      loading = false;
    }
  }

  async function loadStreams() {
    try {
      const r = await listStreams();
      streams = r.streams || [];
    } catch {
      streams = [];
    }
  }

  function setGroupBy(g: GroupBy) {
    groupBy = g;
    try { localStorage.setItem('backlog.groupBy', g); } catch {}
  }

  async function onCreateSubmit({ patch }: { slug: string; patch: BacklogCreateInput }) {
    await createBacklog(patch);
    logEvent(`Backlog: ${patch.slug} created`, 'ok');
    await load();
  }

  async function onEditSubmit({ slug, patch }: { slug: string; patch: BacklogCreateInput }) {
    await patchBacklog(slug, {
      title: patch.title,
      content: patch.content,
      priority: patch.priority,
      impact: patch.impact,
      effort: patch.effort
    });
    logEvent(`Backlog: ${slug} updated`, 'ok');
    await load();
  }

  async function onPromoteSubmit(payload: BacklogPromoteInput) {
    if (!promotingItem) return;
    const r = await promoteBacklog(promotingItem.slug, payload);
    logEvent(`Backlog: ${promotingItem.slug} → task ${r.task_slug} (agent ${r.next_agent})`, 'ok');
    await load();
  }

  async function onBumpPriority(it: BacklogItem, delta: number) {
    const next = Math.max(-2, Math.min(2, it.priority + delta));
    if (next === it.priority) return;
    try {
      await patchBacklog(it.slug, { priority: next });
      await load();
    } catch (e) {
      error = String(e);
    }
  }

  async function onDiscard(slug: string) {
    if (!confirm(`Mark ${slug} as discarded?`)) return;
    try {
      await patchBacklog(slug, { status: 'descartado' });
      await load();
    } catch (e) {
      error = String(e);
    }
  }

  async function onDelete(slug: string) {
    if (!confirm(`Delete ${slug} permanently?`)) return;
    try {
      await deleteBacklog(slug);
      await load();
    } catch (e) {
      error = String(e);
    }
  }

  async function onRevert(it: BacklogItem) {
    if (!confirm(
      `Revert ${it.slug} back to "aberto"? This deletes the task conversation ` +
      `and the task row — useful when you dispatched to the wrong agent and ` +
      `want to re-promote. Refuses if humans have engaged or phases completed.`
    )) return;
    try {
      await revertBacklog(it.slug);
      logEvent(`Backlog: ${it.slug} reverted to aberto`, 'ok');
      await load();
    } catch (e) {
      error = String(e);
    }
  }

  async function onForceReset(it: BacklogItem) {
    const taskRef = it.promoted_task_slug ?? it.slug;
    if (!confirm(
      `FORCE RESET ${it.slug}?\n\n` +
      `This wipes EVERYTHING tied to task "${taskRef}":\n` +
      `  • all conversations and messages (including subconvs from ask_agent)\n` +
      `  • phases, worktrees, orchestrator events\n` +
      `  • artifacts in company/tasks/${taskRef}/\n` +
      `  • the task row itself\n\n` +
      `Backlog item returns to "aberto" so you can re-promote.\n` +
      `This skips all safety checks (phases done, humans engaged).\n\n` +
      `Cannot be undone.`
    )) return;
    if (!confirm(`Last warning: really nuke "${taskRef}"?`)) return;
    try {
      const r = await forceResetBacklog(it.slug);
      logEvent(
        `Backlog: ${it.slug} force-reset` +
          (r.task_slug ? ` (task ${r.task_slug} wiped)` : ' (no task)'),
        'ok',
      );
      await load();
    } catch (e) {
      error = String(e);
    }
  }

  async function onReopen(it: BacklogItem) {
    try {
      await reopenBacklog(it.slug);
      logEvent(`Backlog: ${it.slug} reopened (aberto)`, 'ok');
      await load();
    } catch (e) {
      error = String(e);
    }
  }

  async function onSpecify(it: BacklogItem) {
    try {
      await patchBacklog(it.slug, { status: 'aberto' });
      logEvent(`Backlog: ${it.slug} specified (draft → open)`, 'ok');
      await load();
    } catch (e) {
      error = String(e);
    }
  }

  onMount(() => {
    try {
      const saved = localStorage.getItem('backlog.groupBy') as GroupBy | null;
      if (saved === 'priority' || saved === 'impact' || saved === 'effort') groupBy = saved;
    } catch {}
    load();
    loadStreams();
  });

  function fmtDate(iso: string | null): string {
    if (!iso) return '—';
    try {
      const d = new Date(iso);
      return d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    } catch {
      return iso;
    }
  }

  function priorityClass(p: number): string {
    if (p >= 2) return 'text-accent2 font-semibold';
    if (p === 1) return 'text-warn';
    if (p === 0) return 'text-muted';
    return 'text-muted/70';
  }
</script>

<div class="flex h-full flex-col overflow-hidden">
  <PageHeader title="Backlog">
    {#snippet actions()}
      <select
        bind:value={statusFilter}
        onchange={load}
        class="min-h-tap rounded border border-border bg-panel2 px-2 py-1.5 text-sm"
        aria-label="Status filter"
      >
        <option value="aberto">Open</option>
        <option value="rascunho">Draft</option>
        <option value="em_execucao">In progress</option>
        <option value="promovido">Promoted</option>
        <option value="concluido">Done</option>
        <option value="descartado">Discarded</option>
        <option value="all">All</option>
      </select>
      <button
        type="button"
        class="inline-flex min-h-tap items-center gap-1 rounded bg-accent px-3 py-1.5 text-sm font-medium text-on-accent hover:bg-accent/90"
        onclick={() => (createOpen = true)}
      >
        <Plus class="h-4 w-4" /> New
      </button>
    {/snippet}
  </PageHeader>

  <!-- Group-by tabs -->
  <div class="flex border-b border-border bg-panel2/50 text-sm">
    {#each ['priority', 'impact', 'effort'] as const as g (g)}
      <button
        type="button"
        class="flex-1 min-h-tap px-3 py-2 font-medium capitalize transition-colors"
        class:text-fg={groupBy === g}
        class:text-muted={groupBy !== g}
        class:bg-panel={groupBy === g}
        onclick={() => setGroupBy(g)}
        aria-pressed={groupBy === g}
      >
        {g}
      </button>
    {/each}
  </div>

  <div class="flex-1 overflow-hidden p-4">
    {#if error}
      <div class="mb-3 rounded border border-accent2/40 bg-accent2/10 p-2 text-xs text-accent2">
        {error}
      </div>
    {/if}

    {#if loading}
      <div class="text-xs text-muted">Loading…</div>
    {:else if items.length === 0}
      <div class="py-8 text-center text-xs text-muted">
        {statusFilter === 'aberto'
          ? 'Backlog is empty. Use "New" to add an item.'
          : `No items with status=${statusFilter}.`}
      </div>
    {:else}
      <div class="flex h-full gap-3 overflow-x-auto pb-2">
        {#each columns as col (col.key)}
          <div class="flex h-full min-w-[260px] max-w-[320px] flex-1 flex-col rounded-md border border-border bg-panel/40">
            <div class="sticky top-0 flex items-center justify-between gap-2 rounded-t-md border-b border-border bg-panel2 px-3 py-2">
              <span class="text-xs font-semibold uppercase tracking-wide text-fg">{col.label}</span>
              <span class="rounded bg-panel px-1.5 py-0.5 text-[10px] text-muted">{col.items.length}</span>
            </div>
            <div class="flex-1 space-y-2 overflow-y-auto p-2">
              {#if col.items.length === 0}
                <div class="py-4 text-center text-[11px] text-muted">empty</div>
              {:else}
                {#each col.items as it (it.slug)}
                  <div class="rounded-md border border-border bg-panel2 p-2">
                    <div class="flex items-start justify-between gap-2">
                      <div class="min-w-0 flex-1">
                        <div class="flex items-center gap-1.5 text-sm font-medium">
                          <span class={priorityClass(it.priority)}>
                            {it.priority >= 0 ? '+' : ''}{it.priority}
                          </span>
                          <span class="truncate" title={it.title}>{it.title}</span>
                        </div>
                        <div class="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[10px] text-muted">
                          <span class="font-mono truncate">{it.slug}</span>
                          {#if it.impact}<span>i:{it.impact}</span>{/if}
                          {#if it.effort}<span>e:{it.effort}</span>{/if}
                          <span
                            class="rounded px-1 py-0.5 uppercase"
                            class:bg-panel={it.status !== 'concluido' && it.status !== 'promovido'}
                            class:bg-ok={it.status === 'concluido'}
                            class:text-on-accent={it.status === 'concluido' || it.status === 'promovido'}
                            class:bg-accent={it.status === 'promovido'}
                          >{it.status === 'concluido' ? 'done' : it.status}</span>
                        </div>
                        <div class="mt-1 text-[10px] text-muted/80">
                          {fmtDate(it.updated_at)}
                          {#if it.promoted_task_slug}
                            <span class="text-accent">· → task/{it.promoted_task_slug}</span>
                          {/if}
                        </div>
                      </div>
                    </div>
                    <div class="mt-2 flex items-center justify-between gap-1">
                      <div class="flex gap-0.5">
                        <button
                          type="button"
                          class="inline-flex h-7 w-7 items-center justify-center rounded text-muted hover:bg-panel hover:text-fg disabled:opacity-40"
                          onclick={() => onBumpPriority(it, +1)}
                          title="Bump priority up"
                          aria-label="Bump priority up"
                          disabled={it.priority >= 2}
                        >
                          <ArrowUp class="h-3.5 w-3.5" />
                        </button>
                        <button
                          type="button"
                          class="inline-flex h-7 w-7 items-center justify-center rounded text-muted hover:bg-panel hover:text-fg disabled:opacity-40"
                          onclick={() => onBumpPriority(it, -1)}
                          title="Bump priority down"
                          aria-label="Bump priority down"
                          disabled={it.priority <= -2}
                        >
                          <ArrowDown class="h-3.5 w-3.5" />
                        </button>
                        <button
                          type="button"
                          class="inline-flex h-7 w-7 items-center justify-center rounded text-muted hover:bg-panel hover:text-fg"
                          onclick={() => (editingItem = it)}
                          title="Edit"
                          aria-label="Edit"
                        >
                          <Pencil class="h-3.5 w-3.5" />
                        </button>
                        {#if it.status === 'aberto' || it.status === 'rascunho'}
                          <button
                            type="button"
                            class="inline-flex h-7 w-7 items-center justify-center rounded text-muted hover:bg-panel hover:text-fg"
                            onclick={() => onDiscard(it.slug)}
                            title="Mark as discarded"
                            aria-label="Discard"
                          >
                            <X class="h-3.5 w-3.5" />
                          </button>
                        {:else}
                          <button
                            type="button"
                            class="inline-flex h-7 w-7 items-center justify-center rounded text-muted hover:bg-accent2/20 hover:text-accent2"
                            onclick={() => onDelete(it.slug)}
                            title="Delete permanently"
                            aria-label="Delete"
                          >
                            <Trash2 class="h-3.5 w-3.5" />
                          </button>
                        {/if}
                      </div>
                      {#if it.status === 'aberto'}
                        <button
                          type="button"
                          class="inline-flex h-7 items-center gap-1 rounded bg-accent px-2 text-[11px] font-medium text-on-accent hover:bg-accent/90"
                          onclick={() => (promotingItem = it)}
                          title="Promote to task and dispatch to first agent"
                        >
                          <Play class="h-3 w-3" /> Run
                        </button>
                      {:else if it.status === 'rascunho'}
                        <button
                          type="button"
                          class="inline-flex h-7 items-center gap-1 rounded border border-border px-2 text-[11px] font-medium text-muted hover:bg-panel hover:text-fg"
                          onclick={() => onSpecify(it)}
                          title="Mark as specified — moves draft to 'aberto' (ready for prioritization or Run)"
                        >
                          Specify
                        </button>
                      {:else if it.status === 'promovido' || it.status === 'concluido'}
                        <div class="flex gap-1">
                          {#if it.status === 'promovido'}
                            <button
                              type="button"
                              class="inline-flex h-7 items-center gap-1 rounded border border-border px-2 text-[11px] font-medium text-muted hover:bg-panel hover:text-fg"
                              onclick={() => onRevert(it)}
                              title="Revert to 'aberto' — deletes the task conv and row so you can re-promote (refuses if humans engaged)"
                            >
                              <Undo2 class="h-3 w-3" /> Revert
                            </button>
                          {/if}
                          <button
                            type="button"
                            class="inline-flex h-7 items-center gap-1 rounded border border-accent2/50 px-2 text-[11px] font-medium text-accent2 hover:bg-accent2/10"
                            onclick={() => onForceReset(it)}
                            title="Force reset — wipes the task entirely (artifacts, conv, phases, events) and returns the backlog item to 'aberto'. No safety checks."
                          >
                            <Bomb class="h-3 w-3" /> Force reset
                          </button>
                        </div>
                      {:else if it.status === 'descartado'}
                        <button
                          type="button"
                          class="inline-flex h-7 items-center gap-1 rounded border border-border px-2 text-[11px] font-medium text-muted hover:bg-panel hover:text-fg"
                          onclick={() => onReopen(it)}
                          title="Reopen — move back to 'aberto'"
                        >
                          <RotateCcw class="h-3 w-3" /> Reopen
                        </button>
                      {/if}
                    </div>
                  </div>
                {/each}
              {/if}
            </div>
          </div>
        {/each}
      </div>
    {/if}
  </div>
</div>

<!-- Modals -->
<BacklogFormModal
  mode="create"
  bind:open={createOpen}
  onOpenChange={(v) => (createOpen = v)}
  onSave={onCreateSubmit}
/>

<BacklogFormModal
  mode="edit"
  item={editingItem}
  open={!!editingItem}
  onOpenChange={(v) => {
    if (!v) editingItem = null;
  }}
  onSave={onEditSubmit}
/>

<PromoteTaskModal
  item={promotingItem}
  {streams}
  onOpenChange={(v) => {
    if (!v) promotingItem = null;
  }}
  onPromote={onPromoteSubmit}
/>
