<script lang="ts">
  import type { Snippet } from 'svelte';

  interface Props {
    title: string;
    /** Optional right-aligned actions snippet (buttons, selects, etc). */
    actions?: Snippet;
  }
  let { title, actions }: Props = $props();
</script>

<header
  class="sticky top-0 z-10 flex items-center justify-between gap-2 border-b border-border bg-panel px-3 py-3 md:px-4"
>
  <h1 class="min-w-0 flex-1 truncate text-base font-semibold md:text-sm">{title}</h1>
  {#if actions}
    <div class="flex shrink-0 items-center gap-1">
      {@render actions()}
    </div>
  {/if}
</header>
