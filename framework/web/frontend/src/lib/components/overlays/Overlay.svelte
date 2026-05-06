<script lang="ts">
  import Sheet from '$lib/components/ui/Sheet.svelte';
  import { closeOverlay, type OverlayId } from '$lib/stores/ui';
  import type { Snippet } from 'svelte';

  interface Props {
    id: OverlayId;
    title: string;
    children: Snippet;
    /** Optional right-aligned controls in the header. */
    actions?: Snippet;
    width?: string;
    flush?: boolean;
    /** Lock desktop dialog to 90vh so tab switches don't reflow the chrome. */
    fullHeight?: boolean;
  }
  let { id, title, children, actions, width = 'max-w-3xl', flush = false, fullHeight = false }: Props = $props();

  // Always rendered open; closing means unmounting via the openOverlays store.
  let open = $state(true);

  function handleOpenChange(v: boolean) {
    if (!v) closeOverlay(id);
  }
</script>

<Sheet bind:open onOpenChange={handleOpenChange} {title} {width} {actions} {flush} {fullHeight}>
  {@render children()}
</Sheet>
