<script lang="ts">
  import { onMount } from 'svelte';
  import Sheet from '$lib/components/ui/Sheet.svelte';
  import { streams } from '$lib/stores/streams';
  import {
    listUsers,
    type CustomJob,
    type CustomJobInput,
    type CustomJobPatch,
    type UserOption
  } from '$lib/stores/scheduler';

  type Mode = 'create' | 'edit';

  interface Props {
    mode: Mode;
    /** Required for edit. */
    job?: CustomJob | null;
    open: boolean;
    onOpenChange: (v: boolean) => void;
    onSave: (payload: {
      slug: string;
      create?: CustomJobInput;
      patch?: CustomJobPatch;
    }) => Promise<void>;
  }

  let { mode, job = null, open = $bindable(), onOpenChange, onSave }: Props = $props();

  let slug = $state('');
  let cron = $state('');
  // Initially only post_message — dropdown extensivel quando novas actions
  // entrarem na whitelist humana (scheduler_routes._HUMAN_ACTION_WHITELIST).
  let action = $state<'post_message'>('post_message');
  let stream = $state('');
  let topic = $state('');
  let content = $state('');
  // Sender username (empty string = system-bot default, conv stays hidden
  // from the human's sidebar). Picking a human surfaces the conv there.
  let sender = $state('');
  let description = $state('');
  let enabled = $state(true);
  let saving = $state(false);
  let error = $state<string | null>(null);

  let users = $state<UserOption[]>([]);
  onMount(async () => {
    try {
      users = await listUsers();
    } catch (e) {
      // Non-fatal: dropdown stays empty, falls back to system-bot.
      console.warn('listUsers failed', e);
    }
  });

  $effect(() => {
    if (!open) return;
    if (mode === 'edit' && job) {
      slug = job.slug;
      cron = job.cron;
      action = (job.action as 'post_message') || 'post_message';
      const p = (job.params as Record<string, unknown>) || {};
      stream = typeof p.stream === 'string' ? p.stream : '';
      topic = typeof p.topic === 'string' ? p.topic : '';
      content = typeof p.content === 'string' ? p.content : '';
      sender = typeof p.sender === 'string' ? p.sender : '';
      description = job.description ?? '';
      enabled = job.enabled;
    } else if (mode === 'create') {
      slug = '';
      cron = '';
      action = 'post_message';
      stream = $streams[0]?.name ?? '';
      topic = '';
      content = '';
      sender = '';
      description = '';
      enabled = true;
    }
    error = null;
  });

  const SLUG_RE = /^[a-z0-9][a-z0-9-]*[a-z0-9]$/;
  const canSubmit = $derived(
    !!cron.trim() &&
      !!stream.trim() &&
      !!topic.trim() &&
      !!content.trim() &&
      (mode === 'edit' || SLUG_RE.test(slug.trim()))
  );

  async function onSubmit(e: Event) {
    e.preventDefault();
    if (!canSubmit) return;
    saving = true;
    error = null;
    try {
      const params: Record<string, string> = {
        stream: stream.trim(),
        topic: topic.trim(),
        content: content.trim()
      };
      if (sender.trim()) {
        params.sender = sender.trim();
      }
      if (mode === 'create') {
        await onSave({
          slug: slug.trim(),
          create: {
            slug: slug.trim(),
            cron: cron.trim(),
            action,
            params,
            description: description.trim() || null,
            enabled
          }
        });
      } else {
        await onSave({
          slug,
          patch: {
            cron: cron.trim(),
            params,
            description: description.trim() || null,
            enabled
          }
        });
      }
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
  title={mode === 'create' ? 'New scheduled job' : `Edit ${slug}`}
  width="max-w-xl"
>
  <form onsubmit={onSubmit} class="flex flex-col gap-3">
    {#if mode === 'create'}
      <label class="text-xs">
        <span class="mb-1 block text-muted">Slug (kebab-case, unique)</span>
        <input
          type="text"
          bind:value={slug}
          placeholder="e.g. daily-status-9am"
          required
          pattern="^[a-z0-9][a-z0-9-]*[a-z0-9]$"
          class="min-h-tap w-full rounded border border-border bg-panel px-2 py-1.5 text-sm font-mono"
        />
      </label>
    {:else}
      <div class="text-xs text-muted">
        Slug: <code class="font-mono text-fg">{slug}</code>
      </div>
    {/if}

    <label class="text-xs">
      <span class="mb-1 block text-muted">
        Cron (5 fields: <code>min hour dom month dow</code>)
      </span>
      <input
        type="text"
        bind:value={cron}
        placeholder="0 9 * * MON-FRI"
        required
        class="min-h-tap w-full rounded border border-border bg-panel px-2 py-1.5 text-sm font-mono"
      />
      <span class="mt-0.5 block text-[10px] text-muted">
        Examples: <code>0 9 * * MON-FRI</code> weekdays 09:00 · <code>*/30 * * * *</code> every 30min · <code>0 18 * * *</code> daily 18:00
      </span>
    </label>

    <label class="text-xs">
      <span class="mb-1 block text-muted">Action</span>
      <select
        bind:value={action}
        class="min-h-tap w-full rounded border border-border bg-panel px-2 py-1.5 text-sm"
        disabled={mode === 'edit'}
      >
        <option value="post_message">post_message</option>
      </select>
    </label>

    <div class="grid grid-cols-1 gap-2 xs:grid-cols-2">
      <label class="text-xs">
        <span class="mb-1 block text-muted">Stream</span>
        <select
          bind:value={stream}
          required
          class="min-h-tap w-full rounded border border-border bg-panel px-2 py-1.5 text-sm"
        >
          <option value="" disabled>select…</option>
          {#each $streams as s (s.id)}
            <option value={s.name}>{s.name}</option>
          {/each}
        </select>
      </label>
      <label class="text-xs">
        <span class="mb-1 block text-muted">Topic</span>
        <input
          type="text"
          bind:value={topic}
          placeholder="daily-status"
          required
          class="min-h-tap w-full rounded border border-border bg-panel px-2 py-1.5 text-sm"
        />
      </label>
    </div>

    <label class="text-xs">
      <span class="mb-1 block text-muted">Send as</span>
      <select
        bind:value={sender}
        class="min-h-tap w-full rounded border border-border bg-panel px-2 py-1.5 text-sm"
      >
        <option value="">system-bot (default — conversation hidden from sidebar)</option>
        {#each users as u (u.id)}
          <option value={u.username}>
            {u.username} ({u.kind}{u.full_name && u.full_name !== u.username ? ` · ${u.full_name}` : ''})
          </option>
        {/each}
      </select>
      <span class="mt-0.5 block text-[10px] text-muted">
        Pick a human to make the conversation appear in their sidebar. Pick a
        bot to make the message look like the agent's own initiative.
      </span>
    </label>

    <label class="text-xs">
      <span class="mb-1 block text-muted">Content (markdown)</span>
      <textarea
        bind:value={content}
        rows="6"
        placeholder="The message body that will be posted on each run."
        required
        class="w-full rounded border border-border bg-panel px-2 py-2 text-sm font-mono"
      ></textarea>
    </label>

    <label class="text-xs">
      <span class="mb-1 block text-muted">Description (optional)</span>
      <input
        type="text"
        bind:value={description}
        placeholder="Why this job exists"
        class="min-h-tap w-full rounded border border-border bg-panel px-2 py-1.5 text-sm"
      />
    </label>

    <label class="flex items-center gap-2 text-xs">
      <input type="checkbox" bind:checked={enabled} class="h-4 w-4" />
      <span>Enabled</span>
    </label>

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
