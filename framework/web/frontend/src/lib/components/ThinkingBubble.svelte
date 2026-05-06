<script lang="ts">
  import { Brain } from 'lucide-svelte';
  import type { LiveEvent } from '$lib/api';
  import { fmtClock } from '$lib/services/format';

  interface Props {
    event: LiveEvent;
    agent: string;
  }
  let { event, agent }: Props = $props();
</script>

<div class="flex w-full flex-col gap-1 rounded-md border border-dashed border-border/60 bg-panel2/40 px-3 py-2 text-sm">
  <div class="flex items-baseline justify-between gap-2 text-xs">
    <div class="flex items-center gap-1.5 text-muted">
      <Brain class="h-3 w-3" />
      <strong class="font-medium">{agent}</strong>
      <span class="italic opacity-70">thinking</span>
    </div>
    <span class="text-xs opacity-70">{fmtClock(event.ts)}</span>
  </div>
  <div class="prose prose-invert prose-sm max-w-none whitespace-pre-wrap break-words text-muted italic">
    {event.summary || '—'}
  </div>
</div>
