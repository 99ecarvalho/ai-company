<script lang="ts">
  import type { ComponentType, SvelteComponent } from 'svelte';

  type IconComp = ComponentType<SvelteComponent<{ class?: string }>>;

  interface Props {
    icon: IconComp;
    label: string;
    onclick: (e: MouseEvent) => void;
    active?: boolean;
    /** 'rail' = vertical (desktop), 'bottom' = horizontal (mobile bottom nav). */
    variant?: 'rail' | 'bottom';
    /** Optional badge count (number) or dot (true). */
    badge?: number | boolean;
  }

  let {
    icon: Icon,
    label,
    onclick,
    active = false,
    variant = 'bottom',
    badge
  }: Props = $props();
</script>

<button
  type="button"
  {onclick}
  aria-label={label}
  aria-pressed={active}
  class="relative inline-flex shrink-0 flex-col items-center justify-center gap-0.5 rounded-md transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent
    {variant === 'bottom' ? 'min-h-tap min-w-tap flex-1 px-2 py-1.5' : 'mx-auto h-16 w-rail px-1 py-2'}
    {active ? 'text-accent' : 'text-muted hover:text-fg'}"
>
  <span class="relative flex items-center justify-center">
    <Icon class="h-6 w-6" />
    {#if badge}
      <span
        class="absolute -right-2 -top-1 inline-flex min-h-4 min-w-4 items-center justify-center rounded-full bg-accent2 px-1 text-[10px] font-semibold leading-none text-on-accent"
      >
        {typeof badge === 'number' ? badge : ''}
      </span>
    {/if}
  </span>
  <span class="text-[10px] font-medium leading-tight">{label}</span>
</button>
