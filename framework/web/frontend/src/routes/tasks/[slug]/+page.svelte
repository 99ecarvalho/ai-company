<script lang="ts">
  import { page } from '$app/stores';
  import { goto } from '$app/navigation';
  import {
    ArrowLeft,
    Archive,
    ArchiveRestore,
    Trash2,
    FileText,
    FolderOpen,
    MessageSquare
  } from 'lucide-svelte';
  import PageHeader from '$lib/components/ui/PageHeader.svelte';
  import {
    getTaskTimeline,
    getTaskStats,
    taskTelemetry,
    archiveTask,
    unarchiveTask,
    deleteTask,
    listFiles,
    type TaskTimelineResponse,
    type TaskStats,
    type TaskTelemetryResponse,
    type FileEntry
  } from '$lib/api';
  import { fmtAge, fmtBytes, fmtCost, fmtMs, fmtNum } from '$lib/services/format';
  import {
    fileViewerTarget,
    openOverlay,
    logEvent,
    showFilesAt,
    showConvPanel
  } from '$lib/stores/ui';
  import { tasks, refreshTasks } from '$lib/stores/tasks';

  let slug = $derived(decodeURIComponent($page.params.slug ?? ''));
  let timeline = $state<TaskTimelineResponse | null>(null);
  let stats = $state<TaskStats | null>(null);
  let telem = $state<TaskTelemetryResponse | null>(null);
  let artifactFiles = $state<FileEntry[]>([]);
  let loading = $state(false);
  let error = $state<string | null>(null);

  async function load() {
    loading = true;
    error = null;
    try {
      const [tl, st, tm, files] = await Promise.all([
        getTaskTimeline(slug),
        getTaskStats(slug).catch(() => null),
        taskTelemetry(slug).catch(() => null),
        listFiles(`company/tasks/${slug}`).catch(() => ({ entries: [], path: '' }))
      ]);
      timeline = tl;
      stats = st;
      telem = tm;
      // Only .md files in the Artifacts section; the rest is browsable via the link below.
      artifactFiles = files.entries
        .filter((e) => !e.is_dir && e.name.endsWith('.md'))
        .sort((a, b) => a.name.localeCompare(b.name));
    } catch (e) {
      error = String(e);
      logEvent(`task ${slug}: ${e}`, 'err');
    } finally {
      loading = false;
    }
  }

  // React to slug changes (navigation between tasks without remount).
  $effect(() => {
    if (slug) load();
  });

  function openArtifact(path: string) {
    fileViewerTarget.set({ path });
    openOverlay('fileViewer');
  }

  function browseFolder() {
    showFilesAt(`company/tasks/${slug}`);
  }

  function openConversation(stream: string, topic: string) {
    showConvPanel(`${stream}/${topic}`);
  }

  async function onArchive() {
    if (!timeline?.meta) return;
    const archived = timeline.meta.status && timeline.meta.status !== 'done'
      ? !confirm(`Archive task "${slug}" (status=${timeline.meta.status})?`)
      : false;
    if (archived) return;
    try {
      await archiveTask(slug);
      logEvent(`archived task ${slug}`, 'ok');
      await refreshTasks();
      goto('/tasks');
    } catch (e) {
      logEvent(`archive: ${e}`, 'err');
    }
  }

  async function onUnarchive() {
    try {
      await unarchiveTask(slug);
      logEvent(`unarchived task ${slug}`, 'ok');
      await refreshTasks();
      await load();
    } catch (e) {
      logEvent(`unarchive: ${e}`, 'err');
    }
  }

  async function onDelete() {
    if (!confirm(`Delete task "${slug}" permanently? This removes phases, worktrees and metadata (files on disk stay).`)) return;
    try {
      await deleteTask(slug);
      logEvent(`deleted task ${slug}`, 'ok');
      await refreshTasks();
      goto('/tasks');
    } catch (e) {
      logEvent(`delete: ${e}`, 'err');
    }
  }

  // Derived values for rendering (stable).
  let meta = $derived(timeline?.meta);
  let conversations = $derived(timeline?.conversations ?? []);
  let phases = $derived(meta?.phases ?? []);
  // `archived` isn't in timeline.meta; take it from the task list summary
  // (global polling in +layout keeps it fresh).
  let isArchived = $derived($tasks.find((t) => t.slug === slug)?.archived ?? false);

  // Status class helper (same as TaskCard).
  const statusClass = $derived.by(() => {
    const s = meta?.status ?? 'in_progress';
    if (s === 'done') return 'bg-ok text-on-accent';
    if (s === 'halt' || s === 'human_review') return 'bg-warn text-on-accent';
    return 'bg-accent/20 text-accent';
  });
</script>

<div class="flex h-full flex-col overflow-hidden">
  <PageHeader title={meta?.title || slug}>
    {#snippet actions()}
      <button
        type="button"
        onclick={() => goto('/tasks')}
        aria-label="Back to tasks"
        title="Back to tasks"
        class="inline-flex min-h-tap min-w-tap items-center justify-center rounded-md text-muted hover:bg-panel2 hover:text-fg md:hidden"
      >
        <ArrowLeft class="h-5 w-5" />
      </button>
      {#if isArchived}
        <button
          type="button"
          onclick={onUnarchive}
          class="inline-flex min-h-tap items-center gap-1.5 rounded-md border border-border bg-panel2 px-3 py-1.5 text-sm hover:bg-bg"
          title="Unarchive"
        >
          <ArchiveRestore class="h-4 w-4" />
          <span class="hidden md:inline">Unarchive</span>
        </button>
      {:else}
        <button
          type="button"
          onclick={onArchive}
          class="inline-flex min-h-tap items-center gap-1.5 rounded-md border border-border bg-panel2 px-3 py-1.5 text-sm hover:bg-bg"
          title="Archive"
        >
          <Archive class="h-4 w-4" />
          <span class="hidden md:inline">Archive</span>
        </button>
      {/if}
      <button
        type="button"
        onclick={onDelete}
        class="inline-flex min-h-tap min-w-tap items-center justify-center rounded-md border border-border bg-panel2 px-2 text-sm text-accent2 hover:bg-bg"
        aria-label="Delete"
        title="Delete"
      >
        <Trash2 class="h-4 w-4" />
      </button>
    {/snippet}
  </PageHeader>

  <div class="flex-1 overflow-y-auto p-4">
    {#if loading && !timeline}
      <p class="text-xs text-muted">loading…</p>
    {:else if error}
      <p class="text-xs text-accent2">error: {error}</p>
    {:else if meta}
      <!-- Meta section -->
      <section class="mb-6 flex flex-wrap items-center gap-2 text-xs text-muted">
        <span class="font-mono text-sm text-fg">{slug}</span>
        <span class="rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide {statusClass}">
          {meta.status ?? 'in_progress'}
        </span>
        {#if meta.workflow}
          <span>workflow: <span class="font-mono text-fg">{meta.workflow}</span></span>
        {/if}
        {#if meta.current_step}
          <span>step: <span class="font-mono text-fg">{meta.current_step}</span></span>
        {/if}
        {#if meta.current_agent}
          <span>agent: <span class="font-mono text-fg">{meta.current_agent}</span></span>
        {/if}
      </section>

      <!-- Stats -->
      {#if stats}
        <section class="mb-6 grid grid-cols-2 gap-2 sm:grid-cols-4">
          <div class="rounded-md border border-border bg-panel2 p-3">
            <div class="text-[10px] uppercase text-muted">runs</div>
            <div class="text-lg font-semibold">{fmtNum(stats.runs)}</div>
          </div>
          <div class="rounded-md border border-border bg-panel2 p-3">
            <div class="text-[10px] uppercase text-muted">total cost</div>
            <div class="text-lg font-semibold">{fmtCost(stats.cost_usd)}</div>
          </div>
          <div class="rounded-md border border-border bg-panel2 p-3">
            <div class="text-[10px] uppercase text-muted">total time</div>
            <div class="text-lg font-semibold">{fmtMs(stats.duration_ms)}</div>
          </div>
          <div class="rounded-md border border-border bg-panel2 p-3">
            <div class="text-[10px] uppercase text-muted">turns</div>
            <div class="text-lg font-semibold">{fmtNum(stats.turns)}</div>
          </div>
        </section>
      {/if}

      <!-- Telemetry breakdown (by model / by agent + trace snapshot) -->
      {#if telem && telem.totals.runs > 0}
        <section class="mb-6">
          <h2 class="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">Telemetry breakdown</h2>
          <div class="grid gap-3 md:grid-cols-2">
            {#if telem.by_model.length}
              <div>
                <div class="mb-1 text-[10px] uppercase text-muted">By model</div>
                <table class="w-full text-xs">
                  <thead class="text-muted">
                    <tr class="border-b border-border">
                      <th class="px-2 py-1 text-left">model</th>
                      <th class="px-2 py-1 text-right">runs</th>
                      <th class="px-2 py-1 text-right">cost</th>
                      <th class="px-2 py-1 text-right">avg dur.</th>
                    </tr>
                  </thead>
                  <tbody>
                    {#each telem.by_model as m (m.model)}
                      <tr class="border-b border-border/40">
                        <td class="px-2 py-1 font-mono text-[11px]">{m.model}</td>
                        <td class="px-2 py-1 text-right">{m.runs}</td>
                        <td class="px-2 py-1 text-right">{fmtCost(m.cost_usd)}</td>
                        <td class="px-2 py-1 text-right">{fmtMs(m.avg_duration_ms)}</td>
                      </tr>
                    {/each}
                  </tbody>
                </table>
              </div>
            {/if}
            {#if telem.by_agent.length}
              <div>
                <div class="mb-1 text-[10px] uppercase text-muted">By agent</div>
                <table class="w-full text-xs">
                  <thead class="text-muted">
                    <tr class="border-b border-border">
                      <th class="px-2 py-1 text-left">agent</th>
                      <th class="px-2 py-1 text-right">runs</th>
                      <th class="px-2 py-1 text-right">cost</th>
                      <th class="px-2 py-1 text-right">avg dur.</th>
                    </tr>
                  </thead>
                  <tbody>
                    {#each telem.by_agent as a (a.agent)}
                      <tr class="border-b border-border/40">
                        <td class="px-2 py-1 font-mono text-[11px]">{a.agent}</td>
                        <td class="px-2 py-1 text-right">{a.runs}</td>
                        <td class="px-2 py-1 text-right">{fmtCost(a.cost_usd)}</td>
                        <td class="px-2 py-1 text-right">{fmtMs(a.avg_duration_ms)}</td>
                      </tr>
                    {/each}
                  </tbody>
                </table>
              </div>
            {/if}
          </div>
          {#if telem.trace_snapshot.snapshots > 0}
            <p class="mt-2 text-[10px] text-muted">
              {telem.trace_snapshot.snapshots} run(s) with deleted conversations —
              preserved aggregate: {fmtNum(telem.trace_snapshot.thinking_count)} thinkings,
              {fmtNum(telem.trace_snapshot.tool_use_count)} tool uses.
            </p>
          {/if}
        </section>
      {/if}

      <!-- Phases -->
      <section class="mb-6">
        <h2 class="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">Phases</h2>
        {#if phases.length === 0}
          <p class="text-xs text-muted">No phases recorded yet.</p>
        {:else}
          <div class="overflow-x-auto">
            <table class="w-full text-xs">
              <thead class="text-muted">
                <tr class="border-b border-border">
                  <th class="px-2 py-1.5 text-left">step</th>
                  <th class="px-2 py-1.5 text-left">agent</th>
                  <th class="px-2 py-1.5 text-left">artifact</th>
                  <th class="px-2 py-1.5 text-left">summary</th>
                  <th class="px-2 py-1.5 text-right">timing</th>
                </tr>
              </thead>
              <tbody>
                {#each phases as p, i (i)}
                  <tr class="border-b border-border/40 align-top">
                    <td class="px-2 py-1.5 font-mono">{p.step ?? '—'}</td>
                    <td class="px-2 py-1.5 font-mono">{p.agent ?? '—'}</td>
                    <td class="px-2 py-1.5">
                      {#if p.artifact}
                        <button
                          type="button"
                          class="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-accent hover:bg-accent/10"
                          onclick={() => openArtifact(`company/tasks/${slug}/${p.artifact}`)}
                          title="Open {p.artifact}"
                        >
                          <FileText class="h-3.5 w-3.5" />
                          {p.artifact}
                        </button>
                      {:else}
                        <span class="text-muted">—</span>
                      {/if}
                    </td>
                    <td class="px-2 py-1.5 text-fg/90">
                      {p.summary ? (p.summary.length > 160 ? p.summary.slice(0, 157) + '…' : p.summary) : '—'}
                    </td>
                    <td class="px-2 py-1.5 text-right font-mono text-[10px] text-muted">
                      {#if p.started_at}
                        {fmtAge(new Date(p.started_at).getTime() / 1000)}
                        {#if p.completed_at}
                          → done
                        {:else}
                          · running
                        {/if}
                      {:else}
                        —
                      {/if}
                    </td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
        {/if}
      </section>

      <!-- Artifacts (folder listing) -->
      <section class="mb-6">
        <div class="mb-2 flex items-center justify-between">
          <h2 class="text-xs font-semibold uppercase tracking-wide text-muted">Artifacts</h2>
          <button
            type="button"
            class="inline-flex min-h-tap items-center gap-1 rounded-md border border-border bg-panel2 px-3 py-1 text-xs text-muted hover:bg-bg hover:text-fg"
            onclick={browseFolder}
            title="Browse folder"
          >
            <FolderOpen class="h-3.5 w-3.5" />
            Browse folder
          </button>
        </div>
        {#if artifactFiles.length === 0}
          <p class="text-xs text-muted">No markdown artifacts in <code>company/tasks/{slug}</code>.</p>
        {:else}
          <div class="grid gap-1">
            {#each artifactFiles as f (f.path)}
              <button
                type="button"
                class="flex min-h-tap items-center gap-2 rounded-md border border-border bg-panel2 px-2 py-2 text-left text-sm hover:bg-bg"
                onclick={() => openArtifact(f.path)}
                title="Open {f.name}"
              >
                <FileText class="h-4 w-4 shrink-0 text-muted" />
                <span class="min-w-0 flex-1 truncate">{f.name}</span>
                <span class="text-xs text-muted">{fmtBytes(f.size)}</span>
              </button>
            {/each}
          </div>
        {/if}
      </section>

      <!-- Conversations -->
      <section class="mb-6">
        <h2 class="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">Conversations</h2>
        {#if conversations.length === 0}
          <p class="text-xs text-muted">No conversations attached.</p>
        {:else}
          <div class="grid gap-1">
            {#each conversations as c (c.id)}
              <button
                type="button"
                class="flex min-h-tap items-center gap-2 rounded-md border border-border bg-panel2 px-2 py-2 text-left text-sm hover:bg-bg"
                onclick={() => openConversation(c.stream, c.topic)}
                title="Open conversation"
              >
                <MessageSquare class="h-4 w-4 shrink-0 text-accent" />
                <span class="min-w-0 flex-1 truncate">
                  <span class="font-semibold">#{c.stream}</span>
                  <span class="text-muted"> · </span>
                  <span class="font-mono text-xs">{c.topic}</span>
                </span>
              </button>
            {/each}
          </div>
        {/if}
      </section>
    {/if}
  </div>
</div>
