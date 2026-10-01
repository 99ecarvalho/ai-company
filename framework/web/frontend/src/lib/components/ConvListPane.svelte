<script lang="ts">
  import { Search, X, Trash2 } from 'lucide-svelte';
  import ConvList from './ConvList.svelte';
  import {
    convFilter,
    conversations,
    convQuery,
    refreshConversations
  } from '$lib/stores/conversations';

  // D-96: the "Active (N)" / "Closed (N)" counter counts only roots
  // (parent_conv_id == null). Children left the sidebar — they became chips
  // in the root's header. Counting all would inflate the number with legacy children.
  const rootCount = $derived(
    $conversations.filter((c) => c.parent_conv_id == null).length
  );
  import { closeAllConversations } from '$lib/api';
  import { logEvent, showCapturePanel } from '$lib/stores/ui';

  async function onCloseAll() {
    if (
      !confirm(
        'Close all conversations? (conversations with pending questions will be kept)'
      )
    )
      return;
    try {
      await closeAllConversations();
      logEvent('closed all', 'ok');
      showCapturePanel();
      refreshConversations();
    } catch (e) {
      logEvent(`close all: ${e}`, 'err');
    }
  }
</script>

<aside class="flex h-full w-full flex-col border-r border-border bg-panel md:w-sidebar">
  <header class="flex items-center gap-2 border-b border-border px-3 py-2">
    <h2 class="min-w-0 flex-1 truncate text-sm font-semibold tracking-wide">Conversations</h2>
    <button
      type="button"
      class="inline-flex min-h-9 min-w-9 items-center justify-center rounded-md text-muted transition-colors hover:bg-panel2 hover:text-accent2 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
      onclick={onCloseAll}
      aria-label="Close all conversations"
      title="Close all conversations"
    >
      <Trash2 class="h-4 w-4" />
    </button>
  </header>

  <!-- Inline search: local conversation filter by topic/agent/preview.
       Full-text search of historical messages still lives at /search. -->
  <div class="border-b border-border p-2">
    <div class="relative">
      <Search class="pointer-events-none absolute left-2 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" />
      <input
        type="search"
        bind:value={$convQuery}
        placeholder="Filter conversations…"
        aria-label="Filter conversations"
        class="min-h-tap w-full rounded-md border border-border bg-panel2 py-2 pl-8 pr-8 text-base focus:border-accent focus:outline-none md:text-sm"
      />
      {#if $convQuery}
        <button
          type="button"
          onclick={() => convQuery.set('')}
          aria-label="Clear filter"
          title="Clear"
          class="absolute right-1 top-1/2 inline-flex h-7 w-7 -translate-y-1/2 items-center justify-center rounded text-muted hover:bg-panel hover:text-fg"
        >
          <X class="h-4 w-4" />
        </button>
      {/if}
    </div>
  </div>

  <div class="flex border-b border-border bg-panel2/50 text-sm">
    <button
      type="button"
      class="flex-1 min-h-tap px-3 py-2 font-medium transition-colors"
      class:text-fg={$convFilter === 'active'}
      class:text-muted={$convFilter !== 'active'}
      class:bg-panel={$convFilter === 'active'}
      onclick={() => convFilter.set('active')}
      aria-pressed={$convFilter === 'active'}
      title="Active threads (not archived)"
    >
      Active
      {#if $convFilter === 'active' && rootCount > 0}
        <span class="ml-1 text-xs text-muted">({rootCount})</span>
      {/if}
    </button>
    <button
      type="button"
      class="flex-1 min-h-tap border-l border-border px-3 py-2 font-medium transition-colors"
      class:text-fg={$convFilter === 'closed'}
      class:text-muted={$convFilter !== 'closed'}
      class:bg-panel={$convFilter === 'closed'}
      onclick={() => convFilter.set('closed')}
      aria-pressed={$convFilter === 'closed'}
      title="Manually archived threads"
    >
      Closed
      {#if $convFilter === 'closed' && rootCount > 0}
        <span class="ml-1 text-xs text-muted">({rootCount})</span>
      {/if}
    </button>
  </div>

  <ConvList />
</aside>
