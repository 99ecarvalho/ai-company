<script lang="ts">
  import {
    conversations,
    conversationTree,
    convFilter,
    convQuery
  } from '$lib/stores/conversations';
  import ConvCard from './ConvCard.svelte';

  const emptyText = $derived(
    $convQuery.trim()
      ? `No matches for "${$convQuery.trim()}"`
      : $convFilter === 'closed'
        ? 'No closed threads.'
        : 'No conversations yet'
  );

  const hasAny = $derived($conversations.length > 0);
  const tree = $derived($conversationTree);

  // D-96: sidebar mostra apenas conversas raiz (parent_conv_id IS NULL).
  // Filhas (delegacoes via ask_agent) saem da sidebar e aparecem como
  // chips no header da raiz, com overlay read-only ao clicar. Isso
  // elimina o multi-nesting visual e o problema "respondo no pai ou
  // no filho?". Filhas continuam em tree.byId pra navegacao por chip.
</script>

<div class="flex-1 overflow-y-auto">
  {#if tree.roots.length === 0}
    <div class="px-3 py-6 text-center text-xs text-muted">
      {#if hasAny && $convQuery.trim()}
        No matches for "{$convQuery.trim()}".
      {:else}
        {emptyText}
      {/if}
    </div>
  {:else}
    {#each tree.roots as node (node.conv.db_id)}
      <div class="border-b border-border">
        <ConvCard conv={node.conv} compact={false} parentTitle={null} />
      </div>
    {/each}
  {/if}
</div>
