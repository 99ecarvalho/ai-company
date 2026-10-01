<script lang="ts">
  import { X } from 'lucide-svelte';
  import { fly } from 'svelte/transition';
  import { toasts, dismissToast } from '$lib/stores/toasts';

  const TTL_MS = 5000;

  // Auto-dismiss action: the timer starts when the toast enters, and is
  // cancelled if it's removed manually (X) before the TTL.
  function autoDismiss(_node: HTMLElement, id: number) {
    const handle = setTimeout(() => dismissToast(id), TTL_MS);
    return {
      destroy: () => clearTimeout(handle)
    };
  }
</script>

<!-- Container fixed top-right on desktop, top on mobile (doesn't clash with bottom-nav).
     High z-index to stay above overlays. pointer-events-none on the wrapper
     so it doesn't block clicks outside the toasts; each card re-enables pointer events. -->
<div
  class="pointer-events-none fixed inset-x-0 top-2 z-[100] flex flex-col items-center gap-2 px-2 sm:left-auto sm:right-3 sm:items-end sm:px-0"
  aria-live="polite"
  aria-atomic="false"
>
  {#each $toasts as t (t.id)}
    <div
      class="pointer-events-auto flex w-full max-w-md items-start gap-2 rounded-md border border-red-700 bg-red-600 px-3 py-2 text-sm text-white shadow-lg sm:w-auto sm:min-w-[280px]"
      role="alert"
      transition:fly={{ y: -16, duration: 180 }}
      use:autoDismiss={t.id}
    >
      <span class="flex-1 break-words">{t.msg}</span>
      <button
        type="button"
        class="-mr-1 -mt-0.5 shrink-0 rounded p-0.5 hover:bg-red-700"
        aria-label="Dismiss"
        onclick={() => dismissToast(t.id)}
      >
        <X class="h-4 w-4" />
      </button>
    </div>
  {/each}
</div>
