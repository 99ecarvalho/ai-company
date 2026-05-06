<script lang="ts">
  import type { ComponentType, SvelteComponent } from 'svelte';

  type IconComp = ComponentType<SvelteComponent<{ class?: string }>>;
  type Variant = 'ghost' | 'primary' | 'danger' | 'subtle';
  type Size = 'sm' | 'md' | 'lg' | 'fab';

  interface Props {
    icon: IconComp;
    label: string;
    onclick?: (e: MouseEvent) => void;
    variant?: Variant;
    size?: Size;
    active?: boolean;
    disabled?: boolean;
    title?: string;
    type?: 'button' | 'submit';
    class?: string;
    ariaHaspopup?: 'menu' | 'dialog' | 'true' | 'false';
    ariaExpanded?: boolean;
  }

  let {
    icon: Icon,
    label,
    onclick,
    variant = 'ghost',
    size = 'md',
    active = false,
    disabled = false,
    title,
    type = 'button',
    class: extraClass = '',
    ariaHaspopup,
    ariaExpanded
  }: Props = $props();

  const sizeClasses: Record<Size, string> = {
    sm: 'min-h-9 min-w-9 p-1.5',
    md: 'min-h-tap min-w-tap p-2.5',
    lg: 'min-h-12 min-w-12 p-3',
    fab: 'h-fab w-fab p-3.5 shadow-lg'
  };

  const iconSizeClasses: Record<Size, string> = {
    sm: 'h-4 w-4',
    md: 'h-6 w-6',
    lg: 'h-7 w-7',
    fab: 'h-7 w-7'
  };

  const variantClasses: Record<Variant, string> = {
    ghost: 'text-muted hover:bg-panel2 hover:text-fg',
    subtle: 'text-fg/80 hover:bg-panel2 hover:text-fg',
    primary: 'bg-accent text-on-accent hover:bg-accent/90',
    danger: 'text-muted hover:bg-panel2 hover:text-accent2'
  };

  const activeClasses = 'bg-panel2 text-fg';
</script>

<button
  {type}
  {onclick}
  {disabled}
  title={title ?? label}
  aria-label={label}
  aria-haspopup={ariaHaspopup}
  aria-expanded={ariaExpanded}
  aria-pressed={active || undefined}
  class="inline-flex shrink-0 items-center justify-center rounded-md transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:cursor-not-allowed disabled:opacity-50 {sizeClasses[
    size
  ]} {active && variant === 'ghost' ? activeClasses : variantClasses[variant]} {extraClass}"
>
  <Icon class={iconSizeClasses[size]} />
</button>
