<script lang="ts">
  import PageHeader from '$lib/components/ui/PageHeader.svelte';
  import { logEntries, clearLog } from '$lib/stores/ui';
</script>

<div class="flex h-full flex-col overflow-hidden">
  <PageHeader title="Activity log">
    {#snippet actions()}
      <button
        type="button"
        class="min-h-tap rounded-md border border-border bg-panel2 px-3 py-1.5 text-sm hover:bg-bg"
        onclick={clearLog}
      >Clear</button>
    {/snippet}
  </PageHeader>

  <div class="flex-1 overflow-y-auto p-4">
    {#if $logEntries.length === 0}
      <p class="text-xs text-muted">no activity yet</p>
    {:else}
      <ul class="flex flex-col gap-1 font-mono text-[11px] leading-relaxed">
        {#each $logEntries as e (e.ts + e.msg)}
          <li
            class="border-b border-border/40 pb-1"
            class:text-ok={e.cls === 'ok'}
            class:text-accent2={e.cls === 'err'}
            class:text-muted={e.cls === ''}
          >
            <span class="opacity-60">{new Date(e.ts).toLocaleTimeString()}</span>
            {' '}
            {e.msg}
          </li>
        {/each}
      </ul>
    {/if}
  </div>
</div>
