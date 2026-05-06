<script lang="ts">
  import { Search } from 'lucide-svelte';
  import PageHeader from '$lib/components/ui/PageHeader.svelte';
  import { searchMessages, type SearchResult } from '$lib/api';
  import { fmtClock } from '$lib/services/format';
  import { sanitizeHtml } from '$lib/services/markdown';
  import { showConvPanel } from '$lib/stores/ui';

  let q = $state('');
  let results: SearchResult[] = $state([]);
  let loading = $state(false);
  let error = $state<string | null>(null);
  let timer: ReturnType<typeof setTimeout> | null = null;

  function onInput() {
    if (timer) clearTimeout(timer);
    timer = setTimeout(run, 300);
  }

  async function run() {
    const query = q.trim();
    if (query.length < 2) {
      results = [];
      return;
    }
    loading = true;
    error = null;
    try {
      const r = await searchMessages(query, 30);
      results = r.items;
    } catch (e) {
      error = String(e);
    } finally {
      loading = false;
    }
  }

  function open(item: SearchResult) {
    showConvPanel(item.conv_id);
  }
</script>

<div class="flex h-full flex-col overflow-hidden">
  <PageHeader title="Search messages" />

  <div class="flex-1 overflow-y-auto p-4">
    <div class="relative mb-3">
      <Search class="pointer-events-none absolute left-2 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" />
      <input
        type="search"
        bind:value={q}
        oninput={onInput}
        placeholder="Search across all conversations…"
        class="min-h-tap w-full rounded-md border border-border bg-panel2 py-2 pl-8 pr-2 text-base focus:border-accent focus:outline-none md:text-sm"
      />
    </div>

    {#if loading}
      <p class="text-xs text-muted">searching…</p>
    {:else if error}
      <p class="text-xs text-accent2">error: {error}</p>
    {:else if q.trim().length < 2}
      <p class="text-xs text-muted">Type at least 2 characters.</p>
    {:else if !results.length}
      <p class="text-xs text-muted">No matches for "{q}".</p>
    {:else}
      <ul class="flex flex-col gap-1">
        {#each results as r (r.message_id)}
          <button
            type="button"
            class="flex flex-col gap-0.5 rounded-md border border-border bg-panel2 p-2.5 text-left text-xs hover:border-accent hover:bg-bg"
            onclick={() => open(r)}
          >
            <div class="flex items-baseline justify-between gap-2 text-[10px]">
              <span class="font-semibold text-fg">{r.stream}</span>
              <span class="flex-1 truncate text-muted">{r.topic}</span>
              <span class="text-muted">{r.sender}</span>
              <span class="text-muted">{fmtClock(r.ts)}</span>
            </div>
            <div class="snippet text-fg/90">{@html sanitizeHtml(r.snippet)}</div>
          </button>
        {/each}
      </ul>
    {/if}
  </div>
</div>

<style>
  :global(.snippet mark) {
    background: var(--warn);
    color: var(--bg);
    padding: 0 2px;
    border-radius: 2px;
  }
</style>
