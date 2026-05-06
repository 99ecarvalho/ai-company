<script lang="ts">
  import { Loader2, MessageSquare, ChevronLeft, Archive } from 'lucide-svelte';
  import Sheet from '$lib/components/ui/Sheet.svelte';
  import MessageBubble from '$lib/components/MessageBubble.svelte';
  import { getConversationMessages, type Message } from '$lib/api';
  import { fmtAge } from '$lib/services/format';
  import type { CustomJobRun } from '$lib/stores/scheduler';

  interface Props {
    open: boolean;
    onOpenChange: (v: boolean) => void;
    jobSlug: string;
    runs: CustomJobRun[];
  }

  let { open = $bindable(), onOpenChange, jobSlug, runs }: Props = $props();

  /** Selected run: when null we show the list; otherwise we show the messages
   *  of that run with a back button. Resets to null when the drawer closes. */
  let selected = $state<CustomJobRun | null>(null);
  let messages = $state<Message[]>([]);
  let loading = $state(false);
  let error = $state<string | null>(null);
  let lastLoadedConvId: number | null = null;

  $effect(() => {
    if (!open) {
      // Reset on close so reopening starts from the list view.
      selected = null;
      messages = [];
      error = null;
      lastLoadedConvId = null;
    }
  });

  $effect(() => {
    if (open && selected && selected.id !== lastLoadedConvId) {
      void load(selected.id);
    }
  });

  async function load(convId: number) {
    loading = true;
    error = null;
    try {
      const detail = await getConversationMessages(String(convId));
      messages = detail.messages;
      lastLoadedConvId = convId;
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      loading = false;
    }
  }

  function fmtRunTimestamp(run: CustomJobRun): string {
    // Topic is `<base>-<unix-ts>`; pull the ts off the end and format
    // human-readably. Fall back to created_at if parsing fails.
    const m = run.topic.match(/-(\d+)$/);
    if (m) {
      const ts = parseInt(m[1], 10) * 1000;
      if (!isNaN(ts)) {
        return new Date(ts).toLocaleString();
      }
    }
    return run.created_at ? new Date(run.created_at).toLocaleString() : run.topic;
  }

  const title = $derived(
    selected ? `${jobSlug} · ${fmtRunTimestamp(selected)}` : `${jobSlug} — runs`
  );
</script>

<Sheet
  bind:open
  {onOpenChange}
  {title}
  width="max-w-2xl"
>
  <div class="flex h-full max-h-[80vh] flex-col gap-2">
    {#if !selected}
      <!-- List view: one row per run -->
      {#if runs.length === 0}
        <div class="flex flex-1 flex-col items-center justify-center gap-2 text-xs text-muted">
          <MessageSquare class="h-6 w-6 opacity-50" />
          No runs yet — the job has not fired.
        </div>
      {:else}
        <p class="border-b border-border pb-2 text-xs text-muted">
          {runs.length} run{runs.length === 1 ? '' : 's'} · most recent first
        </p>
        <div class="flex flex-1 flex-col gap-1 overflow-y-auto">
          {#each runs as run (run.id)}
            <button
              type="button"
              class="flex items-center gap-2 rounded border border-border bg-panel2 px-3 py-2 text-left text-sm hover:bg-bg disabled:opacity-50"
              onclick={() => (selected = run)}
            >
              <div class="min-w-0 flex-1">
                <div class="flex items-center gap-2">
                  <span class="font-mono text-fg">{fmtRunTimestamp(run)}</span>
                  {#if run.archived}
                    <span class="inline-flex items-center gap-0.5 rounded bg-panel px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-muted">
                      <Archive class="h-3 w-3" /> archived
                    </span>
                  {/if}
                </div>
                <div class="text-[10px] text-muted">
                  {run.msg_count} msg{run.msg_count === 1 ? '' : 's'}
                  {#if run.last_message_at}
                    · last {fmtAge(Math.floor(new Date(run.last_message_at).getTime() / 1000))} ago
                  {/if}
                </div>
              </div>
              <MessageSquare class="h-4 w-4 text-muted" />
            </button>
          {/each}
        </div>
      {/if}
    {:else}
      <!-- Detail view: messages of the selected run -->
      <div class="flex items-center gap-2 border-b border-border pb-2">
        <button
          type="button"
          class="inline-flex items-center gap-1 rounded border border-border bg-panel2 px-2 py-1 text-xs hover:bg-bg"
          onclick={() => (selected = null)}
        >
          <ChevronLeft class="h-3.5 w-3.5" />
          Back to runs
        </button>
        <span class="text-xs text-muted">
          {selected.stream}/{selected.topic}
        </span>
      </div>

      {#if loading && messages.length === 0}
        <div class="flex flex-1 items-center justify-center text-xs text-muted">
          <Loader2 class="mr-2 h-4 w-4 animate-spin" />
          Loading messages…
        </div>
      {:else if error}
        <div class="rounded border border-warn/40 bg-warn/10 px-3 py-2 text-sm text-warn">
          {error}
        </div>
      {:else if messages.length === 0}
        <div class="flex flex-1 flex-col items-center justify-center gap-2 text-xs text-muted">
          <MessageSquare class="h-6 w-6 opacity-50" />
          No messages in this run yet.
        </div>
      {:else}
        <div class="flex flex-1 flex-col gap-2 overflow-y-auto">
          {#each messages as msg (msg.id)}
            <MessageBubble {msg} />
          {/each}
        </div>
        <div class="border-t border-border pt-2 text-[10px] text-muted">
          {messages.length} message{messages.length === 1 ? '' : 's'}
        </div>
      {/if}
    {/if}
  </div>
</Sheet>
