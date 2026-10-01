<script lang="ts">
  import { Dialog } from 'bits-ui';
  import {
    X,
    Settings,
    Search,
    Folder,
    Brain,
    UserPlus,
    ClipboardList,
    Clock,
    Bell,
    BellOff,
    BellRing,
    Sun,
    Moon,
    LogOut
  } from 'lucide-svelte';
  import { goto } from '$app/navigation';
  import { drawerOpen, openOverlay, status } from '$lib/stores/ui';
  import { authLogout, testPush } from '$lib/api';
  import { pushState, togglePush } from '$lib/stores/push';
  import { theme, toggleTheme } from '$lib/stores/theme';
  import { currentUser } from '$lib/stores/auth';
  import type { OverlayId } from '$lib/stores/ui';
  import type { ComponentType, SvelteComponent } from 'svelte';

  type IconComp = ComponentType<SvelteComponent<{ class?: string }>>;

  function goRoute(path: string) {
    drawerOpen.set(false);
    goto(path);
  }

  function pickOverlay(id: OverlayId) {
    drawerOpen.set(false);
    openOverlay(id);
  }

  async function onLogout() {
    drawerOpen.set(false);
    try {
      await authLogout();
    } catch {
      /* ignore */
    }
    goto('/login', { replaceState: true });
  }

  function onTogglePush() {
    togglePush();
  }

  async function onTestPush() {
    drawerOpen.set(false);
    try {
      await testPush(5);
      status.set('Push test scheduled in 5s — close the app to verify');
    } catch {
      status.set('Push test failed');
    }
  }

  function onToggleTheme() {
    toggleTheme();
  }

  interface Section {
    title: string;
    items: Item[];
  }
  type Item =
    | { kind: 'route'; path: string; icon: IconComp; label: string; description?: string; ariaLabel?: string; mobileOnly?: boolean }
    | { kind: 'overlay'; id: OverlayId; icon: IconComp; label: string; description?: string; ariaLabel?: string; mobileOnly?: boolean }
    | { kind: 'action'; icon: IconComp; label: string; description?: string; onclick: () => void; danger?: boolean; ariaLabel?: string; mobileOnly?: boolean };

  let pushIcon = $derived($pushState === 'enabled' ? Bell : BellOff);
  let pushLabel = $derived(
    $pushState === 'enabled'
      ? 'Notifications: on'
      : $pushState === 'blocked'
        ? 'Notifications: blocked'
        : $pushState === 'unsupported'
          ? 'Notifications: unsupported'
          : 'Notifications: off'
  );
  let themeIcon = $derived($theme === 'dark' ? Sun : Moon);
  let themeLabel = $derived($theme === 'dark' ? 'Theme: light' : 'Theme: dark');

  let sections = $derived<Section[]>([
    {
      title: 'Workspace',
      items: [
        // Mobile-only: Files, Settings, Scheduler already appear in the desktop rail.
        { kind: 'route', path: '/memory', icon: Brain, label: 'Memory', mobileOnly: true },
        { kind: 'route', path: '/files', icon: Folder, label: 'Files', mobileOnly: true },
        { kind: 'route', path: '/scheduler', icon: Clock, label: 'Scheduler', description: 'Cron jobs + manual trigger', mobileOnly: true },
        { kind: 'route', path: '/settings', icon: Settings, label: 'Settings', description: 'System prompts + agent policies', mobileOnly: true },
        { kind: 'route', path: '/search', icon: Search, label: 'Search messages' },
        { kind: 'overlay', id: 'hire', icon: UserPlus, label: 'Hire agent' },
        { kind: 'route', path: '/log', icon: ClipboardList, label: 'Activity log' }
      ]
    },
    {
      title: 'System',
      items: [
        { kind: 'action', icon: pushIcon, label: pushLabel, ariaLabel: 'Toggle push notifications', onclick: onTogglePush },
        ...($pushState === 'enabled'
          ? [{
              kind: 'action' as const,
              icon: BellRing,
              label: 'Send test push (5s)',
              description: 'Close the app to verify it arrives',
              ariaLabel: 'Send test push notification in 5 seconds',
              onclick: onTestPush
            }]
          : []),
        { kind: 'action', icon: themeIcon, label: themeLabel, ariaLabel: 'Toggle theme', onclick: onToggleTheme }
      ]
    },
    {
      title: 'Account',
      items: $currentUser
        ? [
            {
              kind: 'action' as const,
              icon: LogOut,
              label: `Logout (${$currentUser.username})`,
              onclick: onLogout,
              danger: true
            }
          ]
        : []
    }
  ]);

  function onItemClick(item: Item) {
    switch (item.kind) {
      case 'route':
        goRoute(item.path);
        break;
      case 'overlay':
        pickOverlay(item.id);
        break;
      case 'action':
        item.onclick();
        break;
    }
  }
</script>

<Dialog.Root bind:open={$drawerOpen}>
  <Dialog.Portal>
    <Dialog.Overlay class="fixed inset-0 z-40 bg-black/60" />
    <Dialog.Content
      aria-label="Menu"
      class="fixed inset-y-0 right-0 z-50 flex w-full max-w-sm flex-col overflow-hidden border-l border-border bg-panel shadow-2xl"
    >
      <header class="sticky top-0 z-10 flex items-center justify-between gap-2 border-b border-border bg-panel px-3 pt-safe">
        <Dialog.Title class="py-3 text-base font-semibold">Menu</Dialog.Title>
        <Dialog.Close
          aria-label="Close"
          class="inline-flex min-h-tap min-w-tap items-center justify-center rounded-md text-muted transition-colors hover:bg-panel2 hover:text-fg focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          <X class="h-6 w-6" />
        </Dialog.Close>
      </header>

      <div class="flex-1 overflow-y-auto pb-safe">
        {#each sections as section (section.title)}
          <div class="border-b border-border last:border-b-0">
            <h3 class="px-4 pt-4 pb-1 text-[11px] font-semibold uppercase tracking-wide text-muted">
              {section.title}
            </h3>
            <ul>
              {#each section.items as item, i (i)}
                <li class:md:hidden={item.mobileOnly}>
                  <button
                    type="button"
                    onclick={() => onItemClick(item)}
                    aria-label={item.ariaLabel ?? item.label}
                    class="flex w-full min-h-tap items-center gap-3 px-4 py-3 text-left transition-colors hover:bg-panel2 focus:outline-none focus-visible:bg-panel2
                      {item.kind === 'action' && item.danger ? 'text-accent2' : 'text-fg'}"
                  >
                    <span class="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-panel2 text-muted">
                      <item.icon class="h-5 w-5" />
                    </span>
                    <span class="min-w-0 flex-1">
                      <span class="block text-sm font-medium">{item.label}</span>
                      {#if item.description}
                        <span class="block truncate text-xs text-muted">{item.description}</span>
                      {/if}
                    </span>
                  </button>
                </li>
              {/each}
            </ul>
          </div>
        {/each}
      </div>

      <footer class="border-t border-border px-4 py-2 text-[11px] text-muted pb-safe">
        Status: <span class="text-fg">{$status}</span>
      </footer>
    </Dialog.Content>
  </Dialog.Portal>
</Dialog.Root>
