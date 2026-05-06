<script lang="ts">
  import { Plus } from 'lucide-svelte';
  import { showCapturePanel, drawerOpen, closeAllOverlays } from '$lib/stores/ui';

  interface Props {
    /** 'rail' = inside the desktop rail (always visible). 'fab' = floating bottom-right (mobile only). */
    variant: 'rail' | 'fab';
  }
  let { variant }: Props = $props();

  function onClick() {
    closeAllOverlays();
    drawerOpen.set(false);
    showCapturePanel();
  }
</script>

{#if variant === 'rail'}
  <button
    type="button"
    onclick={onClick}
    aria-label="New capture"
    title="New capture"
    class="mx-auto inline-flex h-12 w-12 items-center justify-center rounded-xl bg-accent text-on-accent shadow-md transition-colors hover:bg-accent/90 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
  >
    <Plus class="h-7 w-7" />
  </button>
{:else}
  <button
    type="button"
    onclick={onClick}
    aria-label="New capture"
    title="New capture"
    class="fixed right-4 z-30 inline-flex h-fab w-fab items-center justify-center rounded-full bg-accent text-on-accent shadow-lg transition-colors hover:bg-accent/90 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent md:hidden"
    style="bottom: calc(var(--bottom-nav-h) + env(safe-area-inset-bottom) + 16px);"
  >
    <Plus class="h-7 w-7" />
  </button>
{/if}
