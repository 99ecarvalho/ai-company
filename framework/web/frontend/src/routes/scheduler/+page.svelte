<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import {
    Play, Pause, RotateCw, RefreshCw, Clock, AlertTriangle, Check, Loader2,
    Plus, Pencil, Trash2, MessageSquare
  } from 'lucide-svelte';
  import PageHeader from '$lib/components/ui/PageHeader.svelte';
  import Sheet from '$lib/components/ui/Sheet.svelte';
  import SchedulerFormModal from '$lib/components/SchedulerFormModal.svelte';
  import SchedulerConvDrawer from '$lib/components/SchedulerConvDrawer.svelte';
  import { fmtAge } from '$lib/services/format';
  import {
    schedulerJobs,
    schedulerHealth,
    schedulerError,
    refreshScheduler,
    startSchedulerStream,
    stopSchedulerStream,
    runJobNow,
    pauseJob,
    resumeJob,
    fmtUntil,
    listCustomJobs,
    createCustomJob,
    updateCustomJob,
    deleteCustomJob,
    type SchedulerJob,
    type CustomJob,
    type CustomJobInput,
    type CustomJobPatch
  } from '$lib/stores/scheduler';
  import { logEvent } from '$lib/stores/ui';
  import { refreshStreams } from '$lib/stores/streams';

  let refreshing = $state(false);
  let confirmRunJob = $state<SchedulerJob | null>(null);
  let confirmDeleteJob = $state<CustomJob | null>(null);
  let actionInFlight = $state<string | null>(null);
  let now = $state(Date.now());
  let tickTimer: ReturnType<typeof setInterval> | null = null;

  let customJobs = $state<CustomJob[]>([]);
  let customLoading = $state(false);

  let modalOpen = $state(false);
  let modalMode = $state<'create' | 'edit'>('create');
  let editingJob = $state<CustomJob | null>(null);

  let drawerOpen = $state(false);
  let drawerJob = $state<CustomJob | null>(null);

  onMount(() => {
    startSchedulerStream();
    refreshStreams();
    void refreshCustom();
    tickTimer = setInterval(() => {
      now = Date.now();
    }, 1000);
  });

  onDestroy(() => {
    stopSchedulerStream();
    if (tickTimer) clearInterval(tickTimer);
  });

  async function refreshCustom() {
    customLoading = true;
    try {
      const r = await listCustomJobs();
      customJobs = r.items;
    } catch (e) {
      logEvent(`list custom jobs: ${e}`, 'err');
    } finally {
      customLoading = false;
    }
  }

  async function onRefresh() {
    refreshing = true;
    try {
      await Promise.all([refreshScheduler(), refreshCustom()]);
    } finally {
      refreshing = false;
    }
  }

  // Merge: for each custom job from the DB, take its runtime from schedulerJobs
  // by slug (id). Jobs disabled in the DB don't show up in the runtime.
  const mergedCustomJobs = $derived(
    customJobs.map((c) => {
      const runtime = $schedulerJobs.find((j) => j.id === c.slug) || null;
      return { custom: c, runtime };
    })
  );

  async function onConfirmRun() {
    if (!confirmRunJob) return;
    const id = confirmRunJob.id;
    actionInFlight = id;
    try {
      await runJobNow(id);
    } catch (e) {
      logEvent(`run job: ${e}`, 'err');
    } finally {
      actionInFlight = null;
      confirmRunJob = null;
    }
  }

  async function onPauseResume(job: SchedulerJob) {
    actionInFlight = job.id;
    try {
      if (job.paused) await resumeJob(job.id);
      else await pauseJob(job.id);
    } catch (e) {
      logEvent(`pause/resume: ${e}`, 'err');
    } finally {
      actionInFlight = null;
    }
  }

  function openCreate() {
    modalMode = 'create';
    editingJob = null;
    modalOpen = true;
  }

  function openEdit(job: CustomJob) {
    modalMode = 'edit';
    editingJob = job;
    modalOpen = true;
  }

  function openConv(job: CustomJob) {
    drawerJob = job;
    drawerOpen = true;
  }

  async function onSaveJob(payload: {
    slug: string;
    create?: CustomJobInput;
    patch?: CustomJobPatch;
  }) {
    if (payload.create) {
      await createCustomJob(payload.create);
      logEvent(`created job ${payload.slug}`, 'ok');
    } else if (payload.patch) {
      await updateCustomJob(payload.slug, payload.patch);
      logEvent(`updated job ${payload.slug}`, 'ok');
    }
    await refreshCustom();
    await refreshScheduler();
  }

  async function onConfirmDelete() {
    if (!confirmDeleteJob) return;
    const slug = confirmDeleteJob.slug;
    actionInFlight = slug;
    try {
      await deleteCustomJob(slug);
      logEvent(`deleted job ${slug}`, 'ok');
      await refreshCustom();
      await refreshScheduler();
    } catch (e) {
      logEvent(`delete job: ${e}`, 'err');
    } finally {
      actionInFlight = null;
      confirmDeleteJob = null;
    }
  }

  function nextHuman(j: SchedulerJob | null, _tick: number): string {
    if (!j) return 'disabled';
    if (j.paused) return 'paused';
    return fmtUntil(j.next_run_time);
  }

  function fmtHealthUptime(sec: number): string {
    if (sec < 60) return `${sec}s`;
    if (sec < 3600) return `${Math.floor(sec / 60)}min`;
    if (sec < 86400) {
      const h = Math.floor(sec / 3600);
      const m = Math.floor((sec % 3600) / 60);
      return `${h}h ${m}min`;
    }
    const d = Math.floor(sec / 86400);
    const h = Math.floor((sec % 86400) / 3600);
    return `${d}d ${h}h`;
  }
</script>

<div class="flex h-full flex-col overflow-hidden">
  <PageHeader title="Scheduler">
    {#snippet actions()}
      <button
        type="button"
        class="inline-flex min-h-tap items-center gap-1 rounded-md border border-border bg-panel2 px-3 py-1.5 text-sm hover:bg-bg disabled:opacity-50"
        onclick={onRefresh}
        disabled={refreshing}
        title="Refresh"
      >
        <RefreshCw class="h-4 w-4 {refreshing ? 'animate-spin' : ''}" />
        Refresh
      </button>
      <button
        type="button"
        class="inline-flex min-h-tap items-center gap-1 rounded-md bg-accent px-3 py-1.5 text-sm font-semibold text-on-accent hover:opacity-90"
        onclick={openCreate}
        title="Create new custom job"
      >
        <Plus class="h-4 w-4" />
        New job
      </button>
    {/snippet}
  </PageHeader>

  <div class="flex-1 overflow-y-auto overflow-x-hidden">
    {#if $schedulerHealth}
      <div class="flex flex-wrap items-center gap-x-4 gap-y-1 border-b border-border bg-panel2/50 px-3 py-2 text-xs text-muted">
        <span class="inline-flex items-center gap-1">
          <span class="inline-block h-2 w-2 rounded-full {$schedulerHealth.status === 'ok' ? 'bg-ok' : 'bg-warn'}"></span>
          {$schedulerHealth.status}
        </span>
        <span>uptime: <span class="font-mono text-fg">{fmtHealthUptime($schedulerHealth.uptime_sec)}</span></span>
        <span>jobs: <span class="font-mono text-fg">{$schedulerHealth.jobs_registered}</span></span>
        <span>fires: <span class="font-mono text-fg">{$schedulerHealth.jobs_fired}</span></span>
        {#if $schedulerHealth.last_fire_at}
          <span>last fire: <span class="font-mono text-fg">{fmtAge(Math.floor($schedulerHealth.last_fire_at))} ago</span></span>
        {/if}
        <span class="basis-full text-[10px] italic sm:ml-auto sm:basis-auto">
          Native routines (backup/cleanup/cost): <a href="/settings?tab=routines" class="text-accent underline">Settings → Routines</a>
        </span>
      </div>
    {/if}

    {#if $schedulerError && $schedulerJobs.length === 0}
      <div class="m-3 rounded-md border border-warn/40 bg-warn/10 px-3 py-2 text-sm text-warn">
        <AlertTriangle class="mr-1 inline h-4 w-4" />
        Scheduler error: {$schedulerError}
      </div>
    {/if}

    {#if customLoading && customJobs.length === 0}
      <div class="px-3 py-8 text-center text-xs text-muted">Loading…</div>
    {:else if customJobs.length === 0}
      <div class="px-3 py-8 text-center text-xs text-muted">
        No custom scheduled jobs yet. Click <span class="font-semibold text-fg">New job</span> to create one.
      </div>
    {:else}
      {#each mergedCustomJobs as { custom, runtime } (custom.slug)}
        <div class="flex flex-col gap-1 border-b border-border px-3 py-3">
          <!-- Mobile: title + tags wrap; buttons go to the next line.
               sm+: title + tags + buttons inline with ml-auto. -->
          <div class="flex min-w-0 flex-wrap items-center gap-2">
            <Clock class="h-4 w-4 shrink-0 {custom.enabled ? 'text-accent' : 'text-muted'}" />
            <span class="min-w-0 flex-1 truncate font-mono text-sm font-semibold text-fg sm:flex-none">{custom.slug}</span>
            <span class="rounded bg-accent/20 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-accent">
              {custom.action}
            </span>
            {#if !custom.enabled}
              <span class="rounded bg-panel2 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-muted">disabled</span>
            {:else if runtime?.paused}
              <span class="rounded bg-warn/20 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-warn">paused</span>
            {/if}
            <!-- On mobile the action group wraps onto its own line (basis-full
                 when narrow); on sm+ it stays inline with ml-auto. -->
            <span class="flex w-full flex-wrap items-center gap-1 sm:ml-auto sm:w-auto sm:shrink-0">
              {#if custom.action === 'post_message' && custom.runs && custom.runs.length > 0}
                <button
                  type="button"
                  class="inline-flex h-8 items-center gap-1 rounded-md border border-border bg-panel2 px-2 text-xs hover:bg-bg"
                  onclick={() => openConv(custom)}
                  title="Open run history"
                >
                  <MessageSquare class="h-3.5 w-3.5" />
                  <span class="hidden xs:inline">Runs</span>
                  <span class="font-mono text-muted">({custom.runs.length})</span>
                </button>
              {/if}
              {#if runtime && custom.enabled}
                <button
                  type="button"
                  class="inline-flex h-8 min-w-tap items-center gap-1 rounded-md border border-border bg-panel2 px-2 text-xs hover:bg-bg disabled:opacity-50"
                  onclick={() => (confirmRunJob = runtime)}
                  disabled={actionInFlight === custom.slug}
                  title="Run now"
                  aria-label="Run now"
                >
                  {#if actionInFlight === custom.slug}
                    <Loader2 class="h-3.5 w-3.5 animate-spin" />
                  {:else}
                    <Play class="h-3.5 w-3.5" />
                  {/if}
                  <span class="hidden xs:inline">Run</span>
                </button>
                <button
                  type="button"
                  class="inline-flex h-8 min-w-tap items-center gap-1 rounded-md border border-border bg-panel2 px-2 text-xs hover:bg-bg disabled:opacity-50"
                  onclick={() => onPauseResume(runtime)}
                  disabled={actionInFlight === custom.slug}
                  title={runtime.paused ? 'Resume (runtime-only)' : 'Pause (runtime-only)'}
                  aria-label={runtime.paused ? 'Resume' : 'Pause'}
                >
                  {#if runtime.paused}
                    <RotateCw class="h-3.5 w-3.5" />
                    <span class="hidden xs:inline">Resume</span>
                  {:else}
                    <Pause class="h-3.5 w-3.5" />
                    <span class="hidden xs:inline">Pause</span>
                  {/if}
                </button>
              {/if}
              <button
                type="button"
                class="inline-flex h-8 items-center gap-1 rounded-md border border-border bg-panel2 px-2 text-xs hover:bg-bg"
                onclick={() => openEdit(custom)}
                title="Edit"
                aria-label="Edit"
              >
                <Pencil class="h-3.5 w-3.5" />
                <span class="hidden xs:inline">Edit</span>
              </button>
              <button
                type="button"
                class="inline-flex h-8 items-center gap-1 rounded-md border border-accent2/40 bg-accent2/10 px-2 text-xs text-accent2 hover:bg-accent2/20"
                onclick={() => (confirmDeleteJob = custom)}
                disabled={actionInFlight === custom.slug}
                title="Delete"
                aria-label="Delete"
              >
                <Trash2 class="h-3.5 w-3.5" />
                <span class="hidden xs:inline">Delete</span>
              </button>
            </span>
          </div>

          <div class="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted">
            <span>cron: <span class="font-mono text-fg">{custom.cron}</span></span>
            <span>next: <span class="text-fg">{nextHuman(runtime, now)}</span></span>
            {#if runtime?.next_run_time && !runtime.paused}
              <span class="font-mono text-[10px]">{new Date(runtime.next_run_time).toLocaleString()}</span>
            {/if}
            {#if custom.created_by}
              <span class="ml-auto text-[10px] italic">created by {custom.created_by}</span>
            {/if}
          </div>

          {#if custom.description}
            <p class="text-xs text-muted">{custom.description}</p>
          {/if}

          {#if runtime?.last_fire_at}
            <div class="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted">
              {#if runtime.last_status === 'ok'}
                <span class="inline-flex items-center gap-1 text-ok">
                  <Check class="h-3 w-3" /> last: {fmtAge(Math.floor(runtime.last_fire_at))} ago
                </span>
              {:else if runtime.last_status === 'error'}
                <span class="inline-flex items-center gap-1 text-warn">
                  <AlertTriangle class="h-3 w-3" /> last: {fmtAge(Math.floor(runtime.last_fire_at))} ago, error
                </span>
              {/if}
              {#if runtime.last_duration_ms != null}
                <span>duration: <span class="font-mono text-fg">{runtime.last_duration_ms}ms</span></span>
              {/if}
              {#if runtime.last_error}
                <details class="basis-full">
                  <summary class="cursor-pointer text-warn hover:underline">error: {runtime.last_error.slice(0, 80)}{runtime.last_error.length > 80 ? '…' : ''}</summary>
                  <pre class="mt-1 whitespace-pre-wrap break-all rounded border border-border bg-panel2 p-2 font-mono text-[10px] text-warn">{runtime.last_error}</pre>
                </details>
              {/if}
            </div>
          {/if}

          {#if Object.keys(custom.params).length > 0}
            <div class="flex flex-wrap gap-x-3 gap-y-0.5 text-[10px] text-muted">
              {#each Object.entries(custom.params) as [k, v] (k)}
                <span><span class="font-mono">{k}</span>: <span class="font-mono text-fg">{typeof v === 'string' ? v.slice(0, 80) : JSON.stringify(v)}</span></span>
              {/each}
            </div>
          {/if}
        </div>
      {/each}
    {/if}
  </div>
</div>

<SchedulerFormModal
  mode={modalMode}
  job={editingJob}
  bind:open={modalOpen}
  onOpenChange={(v) => (modalOpen = v)}
  onSave={onSaveJob}
/>

{#if drawerJob && drawerJob.runs}
  <SchedulerConvDrawer
    bind:open={drawerOpen}
    onOpenChange={(v) => {
      drawerOpen = v;
      if (!v) drawerJob = null;
    }}
    jobSlug={drawerJob.slug}
    runs={drawerJob.runs}
  />
{/if}

{#if confirmRunJob}
  {@const j = confirmRunJob}
  <Sheet
    open={true}
    onOpenChange={(v) => {
      if (!v) confirmRunJob = null;
    }}
    title="Run job now?"
    width="max-w-md"
  >
    <div class="flex flex-col gap-3 text-sm">
      <p>Will execute <span class="font-mono font-semibold">{j.id}</span> immediately.</p>
      <div class="mt-2 flex justify-end gap-2">
        <button
          type="button"
          class="inline-flex min-h-tap items-center rounded-md border border-border bg-panel2 px-3 py-1.5 text-sm hover:bg-bg"
          onclick={() => (confirmRunJob = null)}
          disabled={actionInFlight === j.id}
        >
          Cancel
        </button>
        <button
          type="button"
          class="inline-flex min-h-tap items-center gap-1 rounded-md bg-accent px-3 py-1.5 text-sm font-semibold text-on-accent hover:opacity-90 disabled:opacity-50"
          onclick={onConfirmRun}
          disabled={actionInFlight === j.id}
        >
          {#if actionInFlight === j.id}
            <Loader2 class="h-4 w-4 animate-spin" />
          {:else}
            <Play class="h-4 w-4" />
          {/if}
          Run now
        </button>
      </div>
    </div>
  </Sheet>
{/if}

{#if confirmDeleteJob}
  {@const j = confirmDeleteJob}
  <Sheet
    open={true}
    onOpenChange={(v) => {
      if (!v) confirmDeleteJob = null;
    }}
    title="Delete scheduled job?"
    width="max-w-md"
  >
    <div class="flex flex-col gap-3 text-sm">
      <p>
        Permanently delete <span class="font-mono font-semibold">{j.slug}</span>?
      </p>
      <p class="text-muted text-xs">
        The scheduler will unregister it immediately. This cannot be undone.
      </p>
      <div class="mt-2 flex justify-end gap-2">
        <button
          type="button"
          class="inline-flex min-h-tap items-center rounded-md border border-border bg-panel2 px-3 py-1.5 text-sm hover:bg-bg"
          onclick={() => (confirmDeleteJob = null)}
          disabled={actionInFlight === j.slug}
        >
          Cancel
        </button>
        <button
          type="button"
          class="inline-flex min-h-tap items-center gap-1 rounded-md bg-accent2 px-3 py-1.5 text-sm font-semibold text-on-accent hover:opacity-90 disabled:opacity-50"
          onclick={onConfirmDelete}
          disabled={actionInFlight === j.slug}
        >
          {#if actionInFlight === j.slug}
            <Loader2 class="h-4 w-4 animate-spin" />
          {:else}
            <Trash2 class="h-4 w-4" />
          {/if}
          Delete
        </button>
      </div>
    </div>
  </Sheet>
{/if}
