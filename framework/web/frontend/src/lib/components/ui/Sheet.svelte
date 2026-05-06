<script lang="ts">
  import { Dialog } from 'bits-ui';
  import { X } from 'lucide-svelte';
  import type { Snippet } from 'svelte';

  interface Props {
    open: boolean;
    onOpenChange: (v: boolean) => void;
    title: string;
    /** Tailwind max-width class for desktop dialog (e.g. "max-w-3xl"). Mobile is always full-screen. */
    width?: string;
    /** Optional right-aligned controls in the header. */
    actions?: Snippet;
    /** Optional left-aligned snippet (e.g. back button), replaces the close X on mobile. */
    leading?: Snippet;
    children: Snippet;
    /** When true, padding is removed from the body — caller controls it. */
    flush?: boolean;
    /** When true, desktop dialog locks to 90vh instead of being content-sized.
     * Use for overlays with tabs/variable content to keep the chrome stable. */
    fullHeight?: boolean;
  }

  let {
    open = $bindable(),
    onOpenChange,
    title,
    width = 'max-w-3xl',
    actions,
    leading,
    children,
    flush = false,
    fullHeight = false
  }: Props = $props();
</script>

<Dialog.Root bind:open onOpenChange={(v) => onOpenChange(v)}>
  <Dialog.Portal>
    <Dialog.Overlay
      class="fixed inset-0 z-40 hidden bg-black/60 md:block"
    />
    <Dialog.Content
      aria-label={title}
      class="fixed inset-0 z-50 flex flex-col overflow-hidden bg-panel md:inset-auto md:left-1/2 md:top-1/2 md:w-full md:-translate-x-1/2 md:-translate-y-1/2 md:rounded-lg md:border md:border-border md:shadow-2xl {fullHeight ? 'md:h-[90vh]' : 'md:max-h-[90vh]'} {width}"
    >
      <header
        class="sticky top-0 z-10 flex items-center justify-between gap-2 border-b border-border bg-panel px-3 pt-safe md:pt-0 md:px-4"
      >
        <div class="flex min-w-0 flex-1 items-center gap-2 py-2">
          {#if leading}
            {@render leading()}
          {/if}
          <Dialog.Title class="truncate text-base font-semibold md:text-sm">
            {title}
          </Dialog.Title>
        </div>
        <div class="flex shrink-0 items-center gap-1 py-2">
          {#if actions}
            {@render actions()}
          {/if}
          <Dialog.Close
            aria-label="Close"
            class="inline-flex min-h-tap min-w-tap items-center justify-center rounded-md text-muted transition-colors hover:bg-panel2 hover:text-fg focus:outline-none focus-visible:ring-2 focus-visible:ring-accent md:min-h-9 md:min-w-9"
          >
            <X class="h-6 w-6 md:h-5 md:w-5" />
          </Dialog.Close>
        </div>
      </header>
      <div class="flex-1 overflow-y-auto pb-safe {flush ? '' : 'p-4'}">
        {@render children()}
      </div>
    </Dialog.Content>
  </Dialog.Portal>
</Dialog.Root>
