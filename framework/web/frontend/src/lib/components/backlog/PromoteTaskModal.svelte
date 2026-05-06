<script lang="ts">
  import Sheet from '$lib/components/ui/Sheet.svelte';
  import type { BacklogItem, BacklogPromoteInput, StreamInfo } from '$lib/api';
  import { defaultStream } from '$lib/stores/streams';
  import { get } from 'svelte/store';

  interface Props {
    item: BacklogItem | null;
    streams: StreamInfo[];
    onOpenChange: (v: boolean) => void;
    onPromote: (payload: BacklogPromoteInput) => Promise<void>;
  }

  let { item, streams, onOpenChange, onPromote }: Props = $props();

  // Open is derived from item presence — null = closed.
  let open = $derived(!!item);

  let taskSlug = $state('');
  let nextAgent = $state('');
  let workflow = $state('');
  let topic = $state('');
  let submitting = $state(false);
  let error = $state<string | null>(null);

  // Seed quando o item muda (abre). initial agent default =
  // WEB_DEFAULT_STREAM (vindo de /api/streams.default) — normalmente o
  // product-owner, que eh quem produz a triagem formal. Usuario pode
  // sobrescrever antes de confirmar.
  $effect(() => {
    if (!item) return;
    taskSlug = item.slug;
    const def = get(defaultStream);
    nextAgent = def && streams.some((s) => s.name === def) ? def : '';
    workflow = '';
    topic = `task-${item.slug}`;
    error = null;
  });

  async function onSubmit(e: Event) {
    e.preventDefault();
    if (!nextAgent.trim()) {
      error = 'Pick an initial agent';
      return;
    }
    submitting = true;
    error = null;
    try {
      await onPromote({
        task_slug: taskSlug.trim() || undefined,
        next_agent: nextAgent.trim(),
        workflow: workflow.trim() || undefined,
        initial_topic: topic.trim() || undefined
      });
      onOpenChange(false);
    } catch (e) {
      error = String(e);
    } finally {
      submitting = false;
    }
  }
</script>

<Sheet
  {open}
  {onOpenChange}
  title={item ? `Promote "${item.slug}" to task` : 'Promote'}
  width="max-w-md"
>
  <form onsubmit={onSubmit} class="flex flex-col gap-3">
    <label class="text-xs">
      <span class="mb-1 block text-muted">Task slug</span>
      <input
        type="text"
        bind:value={taskSlug}
        class="min-h-tap w-full rounded border border-border bg-panel2 px-2 py-1.5 text-sm"
      />
    </label>

    <label class="text-xs">
      <span class="mb-1 block text-muted">Initial agent *</span>
      <select
        bind:value={nextAgent}
        required
        class="min-h-tap w-full rounded border border-border bg-panel2 px-2 py-1.5 text-sm"
      >
        <option value="">-- pick --</option>
        {#each streams as s (s.name)}
          <option value={s.name}>{s.name}</option>
        {/each}
      </select>
    </label>

    <label class="text-xs">
      <span class="mb-1 block text-muted">Workflow (optional)</span>
      <input
        type="text"
        bind:value={workflow}
        placeholder="leave blank to use default"
        class="min-h-tap w-full rounded border border-border bg-panel2 px-2 py-1.5 text-sm"
      />
    </label>

    <label class="text-xs">
      <span class="mb-1 block text-muted">Initial topic</span>
      <input
        type="text"
        bind:value={topic}
        class="min-h-tap w-full rounded border border-border bg-panel2 px-2 py-1.5 text-sm"
      />
    </label>

    {#if error}
      <p class="text-xs text-accent2">{error}</p>
    {/if}

    <div class="flex justify-end gap-2">
      <button
        type="button"
        class="min-h-tap rounded border border-border px-3 py-1.5 text-sm text-muted hover:bg-panel2"
        onclick={() => onOpenChange(false)}
        disabled={submitting}
      >
        Cancel
      </button>
      <button
        type="submit"
        class="min-h-tap rounded bg-accent px-3 py-1.5 text-sm font-medium text-on-accent hover:bg-accent/90 disabled:opacity-50"
        disabled={submitting || !nextAgent}
      >
        {submitting ? 'Running…' : 'Run now'}
      </button>
    </div>
  </form>
</Sheet>
