<script lang="ts">
  import { ListChecks, Archive } from 'lucide-svelte';
  import type { TaskSummary } from '$lib/api';
  import { fmtAge } from '$lib/services/format';
  import { now } from '$lib/stores/clock';

  interface Props {
    task: TaskSummary;
    onclick: () => void;
  }
  let { task, onclick }: Props = $props();

  // Status visual: done = verde; halt/human_review = warn; archived = muted;
  // in_progress/null = accent neutro.
  const statusClass = $derived.by(() => {
    if (task.archived) return 'bg-panel2 text-muted';
    const s = task.status ?? 'in_progress';
    if (s === 'done') return 'bg-ok text-on-accent';
    if (s === 'halt' || s === 'human_review') return 'bg-warn text-on-accent';
    return 'bg-accent/20 text-accent';
  });

  const updatedTs = $derived.by(() => {
    if (!task.updated_at) return 0;
    return new Date(task.updated_at).getTime() / 1000;
  });
</script>

<button
  type="button"
  {onclick}
  class="flex w-full flex-col gap-1 border-b border-border px-3 py-3 text-left transition-colors hover:bg-panel2 focus:outline-none focus-visible:bg-panel2"
>
  <div class="flex min-w-0 items-center gap-2">
    <ListChecks class="h-4 w-4 shrink-0 text-accent" />
    <span class="truncate font-mono text-xs text-muted">{task.slug}</span>
    {#if task.archived}
      <Archive class="h-3.5 w-3.5 shrink-0 text-muted" aria-label="archived" />
    {/if}
    <span class="min-w-0 flex-1 truncate text-sm font-semibold">{task.title}</span>
  </div>

  <div class="flex flex-wrap items-center gap-x-2 gap-y-1 text-[10px] text-muted">
    <span class="rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide {statusClass}">
      {task.archived ? 'archived' : (task.status ?? 'in_progress')}
    </span>
    {#if task.workflow}
      <span class="font-mono">wf: {task.workflow}</span>
    {/if}
    {#if task.current_step}
      <span>step: <span class="font-mono text-fg">{task.current_step}</span></span>
    {/if}
    {#if task.current_agent}
      <span>agent: <span class="font-mono text-fg">{task.current_agent}</span></span>
    {/if}
    <span>{task.phases_count} phase{task.phases_count === 1 ? '' : 's'}</span>
    {#if updatedTs}
      <span>• {fmtAge(updatedTs, $now)}</span>
    {/if}
  </div>
</button>
