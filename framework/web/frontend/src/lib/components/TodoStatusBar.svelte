<script lang="ts">
  import { ChevronRight, Check, Loader2, Circle } from 'lucide-svelte';
  import type { LiveEvent } from '$lib/api';

  interface Props {
    events: LiveEvent[];
  }
  let { events }: Props = $props();

  // Agent's TodoWrite schema: { todos: [{ content: string, activeForm: string,
  // status: 'pending' | 'in_progress' | 'completed' }] }. Each invocation
  // replaces the whole list; we just render the LAST one.
  interface Todo {
    content: string;
    activeForm?: string;
    status: 'pending' | 'in_progress' | 'completed' | string;
  }

  function parseTodos(ev: LiveEvent): Todo[] | null {
    if (ev.kind !== 'tool_use') return null;
    const tool = typeof ev.data?.tool === 'string' ? (ev.data.tool as string) : '';
    if (!tool.endsWith('TodoWrite')) return null;
    const raw = ev.data?.input;
    let input: Record<string, unknown>;
    if (typeof raw === 'string') {
      try {
        input = JSON.parse(raw);
      } catch {
        return null;
      }
    } else if (raw && typeof raw === 'object') {
      input = raw as Record<string, unknown>;
    } else {
      return null;
    }
    const list = input.todos;
    if (!Array.isArray(list)) return null;
    const out: Todo[] = [];
    for (const t of list) {
      if (t && typeof t === 'object') {
        const item = t as Record<string, unknown>;
        const content = typeof item.content === 'string' ? item.content : '';
        const status = typeof item.status === 'string' ? item.status : 'pending';
        if (!content) continue;
        const activeForm = typeof item.activeForm === 'string' ? item.activeForm : undefined;
        out.push({ content, activeForm, status });
      }
    }
    return out.length > 0 ? out : null;
  }

  const latest = $derived.by<Todo[] | null>(() => {
    // Walk events in reverse to find the last TodoWrite.
    for (let i = events.length - 1; i >= 0; i--) {
      const parsed = parseTodos(events[i]);
      if (parsed) return parsed;
    }
    return null;
  });

  const counts = $derived.by(() => {
    if (!latest) return { done: 0, inProgress: 0, pending: 0, total: 0 };
    let done = 0;
    let inProgress = 0;
    let pending = 0;
    for (const t of latest) {
      if (t.status === 'completed') done++;
      else if (t.status === 'in_progress') inProgress++;
      else pending++;
    }
    return { done, inProgress, pending, total: latest.length };
  });

  let expanded = $state(false);
  function toggle() {
    expanded = !expanded;
  }

  // Auto-collapse quando a lista fica toda done — evita ruído persistente
  // na tela depois do agente finalizar. Usuario pode re-expandir se quiser.
  $effect(() => {
    if (latest && counts.done === counts.total && counts.total > 0) {
      expanded = false;
    }
  });
</script>

{#if latest}
  <div class="border-t border-border bg-panel2/60 text-xs">
    <button
      type="button"
      onclick={toggle}
      class="flex w-full items-center gap-2 px-3 py-1.5 text-left hover:bg-panel2"
      aria-expanded={expanded}
    >
      <ChevronRight class="h-3 w-3 shrink-0 text-muted transition-transform {expanded ? 'rotate-90' : ''}" />
      <span class="font-mono text-[11px] text-muted">
        todos: <span class="text-fg">{counts.done} / {counts.total}</span>
        {#if counts.inProgress > 0}
          <span class="text-accent"> · {counts.inProgress} in progress</span>
        {/if}
        {#if counts.pending > 0}
          <span class="text-muted"> · {counts.pending} pending</span>
        {/if}
      </span>
      {#if counts.done < counts.total}
        <div class="ml-auto flex items-center gap-1">
          <div class="h-1 w-20 overflow-hidden rounded-full bg-panel">
            <div
              class="h-full bg-accent transition-all"
              style="width: {counts.total > 0 ? Math.round((counts.done / counts.total) * 100) : 0}%"
            ></div>
          </div>
        </div>
      {/if}
    </button>
    {#if expanded}
      <ul class="max-h-[180px] overflow-y-auto border-t border-border bg-bg px-3 py-1.5">
        {#each latest as todo, i (i + '-' + todo.content.slice(0, 30))}
          <li class="flex items-start gap-2 py-0.5 text-[11px]">
            <span class="mt-0.5 shrink-0">
              {#if todo.status === 'completed'}
                <Check class="h-3 w-3 text-ok" />
              {:else if todo.status === 'in_progress'}
                <Loader2 class="h-3 w-3 animate-spin text-accent" />
              {:else}
                <Circle class="h-3 w-3 text-muted" />
              {/if}
            </span>
            <span
              class="flex-1 break-words"
              class:line-through={todo.status === 'completed'}
              class:text-muted={todo.status === 'completed'}
              class:text-accent={todo.status === 'in_progress'}
              class:font-semibold={todo.status === 'in_progress'}
            >
              {todo.status === 'in_progress' && todo.activeForm ? todo.activeForm : todo.content}
            </span>
          </li>
        {/each}
      </ul>
    {/if}
  </div>
{/if}
