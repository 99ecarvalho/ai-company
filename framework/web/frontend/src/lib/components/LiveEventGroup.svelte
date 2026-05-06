<script lang="ts">
  import { ChevronRight, Wrench } from 'lucide-svelte';
  import LiveEventLine from './LiveEventLine.svelte';
  import type { LiveEvent, WorkflowDef } from '$lib/api';
  import { fmtClock } from '$lib/services/format';

  interface Props {
    events: LiveEvent[];
    results: (LiveEvent | null)[];
    tool: string;
    workflowDef?: WorkflowDef | null;
  }
  let { events, results, tool, workflowDef = null }: Props = $props();

  let expanded = $state(false);
  function toggle() {
    expanded = !expanded;
  }

  function basename(p: string): string {
    const slash = p.lastIndexOf('/');
    return slash >= 0 ? p.slice(slash + 1) : p;
  }

  function clamp(s: string, n: number): string {
    return s.length > n ? s.slice(0, n - 1) + '…' : s;
  }

  // Extract a short hint per event — basename(file_path) for filesystem tools,
  // first kv value for others. Used in the collapsed header preview.
  function shortHint(ev: LiveEvent): string {
    const raw = ev.data?.input;
    let inp: Record<string, unknown> = {};
    if (typeof raw === 'string') {
      try {
        inp = JSON.parse(raw);
      } catch {
        return '';
      }
    } else if (raw && typeof raw === 'object') {
      inp = raw as Record<string, unknown>;
    }
    if (typeof inp.file_path === 'string') return basename(inp.file_path);
    if (typeof inp.pattern === 'string') return clamp(inp.pattern, 30);
    if (typeof inp.command === 'string') return clamp(inp.command, 30);
    if (typeof inp.description === 'string') return clamp(inp.description, 30);
    if (typeof inp.query === 'string') return clamp(inp.query, 30);
    const first = Object.values(inp).find((v) => typeof v === 'string') as string | undefined;
    return first ? clamp(first, 30) : '';
  }

  const previewHints = $derived.by(() => {
    const hints = events.map(shortHint).filter(Boolean);
    if (hints.length === 0) return '';
    const head = hints.slice(0, 3).join(', ');
    return hints.length > 3 ? `${head} +${hints.length - 3} more` : head;
  });

  const lastTs = $derived(events[events.length - 1]?.ts ?? events[0]?.ts ?? 0);
</script>

<div class="flex flex-col font-mono text-[11px] text-muted">
  <div class="flex w-full items-start gap-2 px-2 py-0.5 hover:bg-panel2/40">
    <button
      type="button"
      class="flex flex-1 items-start gap-2 text-left min-w-0 cursor-pointer"
      onclick={toggle}
    >
      <ChevronRight
        class="mt-0.5 h-3 w-3 shrink-0 text-warn transition-transform {expanded ? 'rotate-90' : ''}"
      />
      <span class="w-[76px] shrink-0 text-[9px] uppercase opacity-70">tool_use ×{events.length}</span>
      <span class="flex-1 truncate opacity-90">
        <span class="font-medium">{tool}</span>
        {#if previewHints}
          <span class="opacity-70"> · {previewHints}</span>
        {/if}
      </span>
    </button>
    <span class="shrink-0 text-[9px] opacity-50 pt-0.5">
      {fmtClock(events[0].ts)}
      {#if events.length > 1}
        <span class="opacity-70">→ {fmtClock(lastTs)}</span>
      {/if}
    </span>
  </div>

  {#if expanded}
    <div class="ml-4 border-l border-border pl-1">
      {#each events as ev, i (ev.id)}
        <LiveEventLine event={ev} result={results[i]} {workflowDef} />
      {/each}
    </div>
  {/if}
</div>
