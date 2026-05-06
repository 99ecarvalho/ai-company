<script lang="ts">
  import { onDestroy, tick, untrack } from 'svelte';
  import { ExternalLink } from 'lucide-svelte';
  import Overlay from './Overlay.svelte';
  import MessageBubble from '$lib/components/MessageBubble.svelte';
  import ThinkingBubble from '$lib/components/ThinkingBubble.svelte';
  import LiveEventLine from '$lib/components/LiveEventLine.svelte';
  import LiveEventGroup from '$lib/components/LiveEventGroup.svelte';
  import {
    childConvTarget,
    closeOverlay,
    logEvent,
    showConvPanel
  } from '$lib/stores/ui';
  import {
    getConversationMessages,
    liveRecent,
    liveStreamUrl,
    type ConversationDetail,
    type LiveEvent
  } from '$lib/api';
  import { buildFeed } from '$lib/services/feed';
  import { fmtDateSeparator, sameDay } from '$lib/services/format';

  // Snapshot do convId no momento de abrir. Se o store muda enquanto aberto,
  // o overlay reseta tudo (load + SSE) pra nova conv. Quando fecha,
  // childConvTarget vira null mas mantemos o ultimo carregado pro fade-out
  // nao mostrar conteudo vazio.
  let convId = $state<string | null>(null);
  let detail = $state<ConversationDetail | null>(null);
  let events = $state<LiveEvent[]>([]);
  let loading = $state(false);
  let error = $state<string | null>(null);
  let listEl: HTMLDivElement | null = $state(null);
  let stickToBottom = true;
  let es: EventSource | null = null;

  const MAX_EVENTS = 100;

  $effect(() => {
    const target = $childConvTarget;
    if (!target) return;
    const next = target.convId;
    untrack(() => {
      if (next === convId) return;
      convId = next;
      detail = null;
      events = [];
      error = null;
      stickToBottom = true;
      teardownStream();
      load(next);
      openStream(next);
    });
  });

  async function load(id: string) {
    loading = true;
    try {
      const [d, recent] = await Promise.all([
        getConversationMessages(id),
        liveRecent(id, 50).catch(() => ({ items: [] as LiveEvent[] }))
      ]);
      if (convId !== id) return;
      detail = d;
      events = recent.items;
      await tick();
      scrollToBottom();
    } catch (e) {
      if (convId !== id) return;
      error = String(e);
      logEvent(`child conv: ${e}`, 'err');
    } finally {
      if (convId === id) loading = false;
    }
  }

  function openStream(id: string) {
    if (typeof EventSource === 'undefined') return;
    es = new EventSource(liveStreamUrl(id));
    es.onmessage = (msg) => {
      try {
        const data = JSON.parse(msg.data) as LiveEvent;
        events = (() => {
          const next = [...events, data];
          if (next.length > MAX_EVENTS) next.splice(0, next.length - MAX_EVENTS);
          return next;
        })();
        if (stickToBottom) {
          tick().then(scrollToBottom);
        }
      } catch {
        /* ignore malformed payload */
      }
    };
    es.onerror = () => {
      /* EventSource auto-reconnect; no-op */
    };
  }

  function teardownStream() {
    if (es) {
      es.close();
      es = null;
    }
  }

  function scrollToBottom() {
    if (listEl) listEl.scrollTop = listEl.scrollHeight;
  }

  function onListScroll() {
    if (!listEl) return;
    const dist = listEl.scrollHeight - listEl.scrollTop - listEl.clientHeight;
    stickToBottom = dist <= 4;
  }

  function openFull() {
    if (!convId) return;
    closeOverlay('childConv');
    showConvPanel(convId);
  }

  onDestroy(() => {
    teardownStream();
    childConvTarget.set(null);
  });

  const feed = $derived(buildFeed(detail?.messages ?? [], events));
  const headerTitle = $derived(detail?.agent || 'delegated');
  const headerMeta = $derived(detail ? `#${detail.stream} · ${detail.topic}` : '');
</script>

<Overlay id="childConv" title={headerTitle} width="max-w-3xl" flush fullHeight>
  {#snippet actions()}
    {#if convId}
      <button
        type="button"
        onclick={openFull}
        class="inline-flex items-center gap-1 rounded-md border border-border bg-panel2 px-2 py-1 text-xs hover:bg-bg"
        title="Open full conversation"
        aria-label="Open full conversation"
      >
        <ExternalLink class="h-3 w-3" />
        <span class="hidden sm:inline">Open full</span>
      </button>
    {/if}
  {/snippet}

  <div class="flex h-full flex-col">
    {#if headerMeta}
      <div class="border-b border-border bg-panel2/40 px-3 py-1.5 text-[11px] text-muted">
        {headerMeta}
        <span class="ml-2 rounded-sm bg-warn/15 px-1.5 py-0.5 text-[10px] uppercase tracking-wider text-warn">
          read-only
        </span>
      </div>
    {/if}
    <div
      bind:this={listEl}
      onscroll={onListScroll}
      class="flex flex-1 flex-col gap-2 overflow-y-auto overflow-x-hidden p-3"
      style="overscroll-behavior: contain;"
    >
      {#if error}
        <div class="rounded-md border border-accent2/40 bg-accent2/10 px-3 py-2 text-xs text-accent2">
          {error}
        </div>
      {:else if loading && !detail}
        <div class="text-xs text-muted">loading…</div>
      {:else if detail}
        {#each feed as item, i (item.id)}
          {@const showDateSeparator = i === 0 || !sameDay(feed[i - 1].ts, item.ts)}
          {#if showDateSeparator}
            <div class="flex items-center gap-2 py-1 text-[10px] uppercase tracking-wider text-muted">
              <span class="h-px flex-1 bg-border"></span>
              <span class="rounded-full bg-panel2 px-2 py-0.5">{fmtDateSeparator(item.ts)}</span>
              <span class="h-px flex-1 bg-border"></span>
            </div>
          {/if}
          {#if item.kind === 'msg'}
            <MessageBubble msg={item.msg} />
          {:else if item.kind === 'thinking'}
            <ThinkingBubble event={item.event} agent={headerTitle} />
          {:else if item.kind === 'tool_use'}
            <LiveEventLine event={item.event} result={item.result} />
          {:else if item.kind === 'tool_use_group'}
            <LiveEventGroup
              events={item.events}
              results={item.results}
              tool={item.tool}
            />
          {:else}
            <LiveEventLine event={item.event} />
          {/if}
        {/each}
      {/if}
    </div>
  </div>
</Overlay>
