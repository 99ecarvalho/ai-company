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

  // D-96: the sidebar shows only root conversations (parent_conv_id IS NULL).
  // Children (delegations via ask_agent) leave the sidebar and appear as
  // chips in the root's header, with a read-only overlay on click. This
  // removes the visual multi-nesting and the "do I reply on the parent or
  // the child?" problem. Children stay in tree.byId for chip navigation.
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
