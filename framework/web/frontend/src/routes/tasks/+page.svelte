<script lang="ts">
  import { Search, X, RefreshCw } from 'lucide-svelte';
  import { goto } from '$app/navigation';
  import PageHeader from '$lib/components/ui/PageHeader.svelte';
  import TaskCard from '$lib/components/TaskCard.svelte';
  import {
    tasks,
    taskFilter,
    taskQuery,
    visibleTasks,
    refreshTasks,
    type TaskFilter
  } from '$lib/stores/tasks';

  let refreshing = $state(false);

  async function onRefresh() {
    refreshing = true;
    try {
      await refreshTasks();
    } finally {
      refreshing = false;
    }
  }

  function openTask(slug: string) {
    goto(`/tasks/${encodeURIComponent(slug)}`);
  }

  function setFilter(f: TaskFilter) {
    taskFilter.set(f);
  }
</script>

<div class="flex h-full flex-col overflow-hidden">
  <PageHeader title="Tasks">
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
    {/snippet}
  </PageHeader>

  <div class="flex-1 overflow-y-auto">
    <!-- Search local -->
    <div class="border-b border-border p-2">
      <div class="relative">
        <Search class="pointer-events-none absolute left-2 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" />
        <input
          type="search"
          bind:value={$taskQuery}
          placeholder="Filter by slug, title, workflow, agent…"
          aria-label="Filter tasks"
          class="min-h-tap w-full rounded-md border border-border bg-panel2 py-2 pl-8 pr-8 text-base focus:border-accent focus:outline-none md:text-sm"
        />
        {#if $taskQuery}
          <button
            type="button"
            onclick={() => taskQuery.set('')}
            aria-label="Clear filter"
            title="Clear"
            class="absolute right-1 top-1/2 inline-flex h-7 w-7 -translate-y-1/2 items-center justify-center rounded text-muted hover:bg-panel hover:text-fg"
          >
            <X class="h-4 w-4" />
          </button>
        {/if}
      </div>
    </div>

    <!-- Filter tabs -->
    <div class="flex border-b border-border bg-panel2/50 text-sm">
      {#each ['active', 'done', 'archived'] as const as f (f)}
        <button
          type="button"
          class="flex-1 min-h-tap px-3 py-2 font-medium capitalize transition-colors"
          class:text-fg={$taskFilter === f}
          class:text-muted={$taskFilter !== f}
          class:bg-panel={$taskFilter === f}
          onclick={() => setFilter(f)}
          aria-pressed={$taskFilter === f}
        >
          {f}
        </button>
      {/each}
    </div>

    <!-- List -->
    {#if $visibleTasks.length === 0}
      <div class="px-3 py-8 text-center text-xs text-muted">
        {#if $tasks.length === 0}
          No tasks yet.
        {:else if $taskQuery.trim()}
          No matches for "{$taskQuery.trim()}".
        {:else if $taskFilter === 'archived'}
          No archived tasks.
        {:else if $taskFilter === 'done'}
          No completed tasks.
        {:else}
          No active tasks.
        {/if}
      </div>
    {:else}
      {#each $visibleTasks as t (t.slug)}
        <TaskCard task={t} onclick={() => openTask(t.slug)} />
      {/each}
    {/if}
  </div>
</div>
