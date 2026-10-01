<script lang="ts">
  import Sheet from '$lib/components/ui/Sheet.svelte';
  import type { BacklogItem, BacklogCreateInput } from '$lib/api';

  type Mode = 'create' | 'edit';

  interface Props {
    mode: Mode;
    /** Required for edit; ignored in create mode. */
    item?: BacklogItem | null;
    open: boolean;
    onOpenChange: (v: boolean) => void;
    /** Return true if saved successfully (modal closes); false/throw keeps it open. */
    onSave: (payload: { slug: string; patch: BacklogCreateInput }) => Promise<void>;
  }

  let { mode, item, open = $bindable(), onOpenChange, onSave }: Props = $props();

  let slug = $state('');
  let title = $state('');
  let content = $state('');
  let priority = $state(0);
  let impact = $state<'' | 'alto' | 'medio' | 'baixo'>('');
  let effort = $state<'' | 'alto' | 'medio' | 'baixo'>('');
  let saving = $state(false);
  let error = $state<string | null>(null);

  // Seed/reset when opening (edit) or switching items.
  $effect(() => {
    if (!open) return;
    if (mode === 'edit' && item) {
      slug = item.slug;
      title = item.title;
      content = item.content ?? '';
      priority = item.priority;
      impact = (item.impact as typeof impact) || '';
      effort = (item.effort as typeof effort) || '';
    } else if (mode === 'create') {
      slug = '';
      title = '';
      content = '';
      priority = 0;
      impact = '';
      effort = '';
    }
    error = null;
  });

  const canSubmit = $derived(
    !!title.trim() && (mode === 'edit' || !!slug.trim())
  );

  async function onSubmit(e: Event) {
    e.preventDefault();
    if (!canSubmit) return;
    saving = true;
    error = null;
    try {
      const payload: BacklogCreateInput = {
        slug: slug.trim().toLowerCase(),
        title: title.trim(),
        content: content.trim() || undefined,
        priority,
        impact: impact || undefined,
        effort: effort || undefined
      };
      await onSave({ slug: payload.slug, patch: payload });
      onOpenChange(false);
    } catch (e) {
      error = String(e);
    } finally {
      saving = false;
    }
  }
</script>

<Sheet
  bind:open
  {onOpenChange}
  title={mode === 'create' ? 'New backlog item' : 'Edit backlog item'}
  width="max-w-xl"
>
  <form onsubmit={onSubmit} class="flex flex-col gap-3">
    {#if mode === 'create'}
      <label class="text-xs">
        <span class="mb-1 block text-muted">Slug (kebab-case)</span>
        <input
          type="text"
          bind:value={slug}
          placeholder="e.g. refactor-payments"
          required
          class="min-h-tap w-full rounded border border-border bg-panel px-2 py-1.5 text-sm"
        />
      </label>
    {:else}
      <div class="text-xs text-muted">
        Slug: <code class="font-mono text-fg">{slug}</code>
      </div>
    {/if}

    <label class="text-xs">
      <span class="mb-1 block text-muted">Title</span>
      <input
        type="text"
        bind:value={title}
        placeholder="Short description"
        required
        class="min-h-tap w-full rounded border border-border bg-panel px-2 py-1.5 text-sm"
      />
    </label>

    <label class="text-xs">
      <span class="mb-1 block text-muted">Context (markdown)</span>
      <textarea
        bind:value={content}
        rows="5"
        placeholder="Motivation, acceptance criteria…"
        class="w-full rounded border border-border bg-panel px-2 py-2 text-sm font-mono"
      ></textarea>
    </label>

    <div class="grid grid-cols-1 gap-2 xs:grid-cols-3">
      <label class="text-xs">
        <span class="mb-1 block text-muted">Priority</span>
        <select
          bind:value={priority}
          class="min-h-tap w-full rounded border border-border bg-panel px-2 py-1.5 text-sm"
        >
          <option value={-2}>-2 very-low</option>
          <option value={-1}>-1 low</option>
          <option value={0}>0 normal</option>
          <option value={1}>+1 high</option>
          <option value={2}>+2 critical</option>
        </select>
      </label>
      <label class="text-xs">
        <span class="mb-1 block text-muted">Impact</span>
        <select
          bind:value={impact}
          class="min-h-tap w-full rounded border border-border bg-panel px-2 py-1.5 text-sm"
        >
          <option value="">—</option>
          <option value="alto">high</option>
          <option value="medio">medium</option>
          <option value="baixo">low</option>
        </select>
      </label>
      <label class="text-xs">
        <span class="mb-1 block text-muted">Effort</span>
        <select
          bind:value={effort}
          class="min-h-tap w-full rounded border border-border bg-panel px-2 py-1.5 text-sm"
        >
          <option value="">—</option>
          <option value="alto">high</option>
          <option value="medio">medium</option>
          <option value="baixo">low</option>
        </select>
      </label>
    </div>

    {#if error}
      <p class="text-xs text-accent2">{error}</p>
    {/if}

    <div class="flex justify-end gap-2">
      <button
        type="button"
        class="min-h-tap rounded border border-border px-3 py-1.5 text-sm text-muted hover:bg-panel2"
        onclick={() => onOpenChange(false)}
        disabled={saving}
      >
        Cancel
      </button>
      <button
        type="submit"
        class="min-h-tap rounded bg-accent px-3 py-1.5 text-sm font-medium text-on-accent hover:bg-accent/90 disabled:opacity-50"
        disabled={!canSubmit || saving}
      >
        {saving ? 'Saving…' : 'Save'}
      </button>
    </div>
  </form>
</Sheet>
