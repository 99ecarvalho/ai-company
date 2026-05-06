<script lang="ts">
  import { X, Paperclip } from 'lucide-svelte';
  import type { UploadResponse } from '$lib/api';

  interface Props {
    items: UploadResponse[];
    onRemove: (item: UploadResponse) => void;
  }

  let { items, onRemove }: Props = $props();
</script>

{#if items.length}
  <div class="flex flex-wrap gap-1.5">
    {#each items as item}
      <span
        class="inline-flex items-center gap-1.5 rounded-md bg-panel2 border border-border px-2 py-1 text-xs"
      >
        <Paperclip class="h-3 w-3 text-muted" />
        <span class="max-w-[180px] truncate" title={item.path}>{item.name}</span>
        <button
          type="button"
          class="ml-1 rounded p-0.5 text-muted hover:bg-bg hover:text-fg"
          onclick={() => onRemove(item)}
          aria-label="Remove attachment"
        >
          <X class="h-3 w-3" />
        </button>
      </span>
    {/each}
  </div>
{/if}
