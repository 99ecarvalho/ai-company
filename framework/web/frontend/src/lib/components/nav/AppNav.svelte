<script lang="ts">
  import {
    MessageSquare,
    ListChecks,
    ClipboardList,
    BarChart3,
    Brain,
    Folder,
    Settings,
    Menu,
    Clock
  } from 'lucide-svelte';
  import { page } from '$app/stores';
  import { goto } from '$app/navigation';
  import { drawerOpen, showListOnMobile } from '$lib/stores/ui';
  import NavItem from './NavItem.svelte';
  import CaptureFab from './CaptureFab.svelte';

  interface Props {
    variant: 'rail' | 'bottom';
  }
  let { variant }: Props = $props();

  // Cast: SvelteKit types pathname as a narrow union of the known routes.
  let pathname = $derived($page.url.pathname as string);

  // Active tab: drawer first; then matching by pathname.
  let convActive = $derived(
    !$drawerOpen && (pathname === '/' || pathname.startsWith('/c/'))
  );
  let tasksActive = $derived(
    !$drawerOpen && (pathname === '/tasks' || pathname.startsWith('/tasks/'))
  );
  let backlogActive = $derived(!$drawerOpen && pathname === '/backlog');
  let schedulerActive = $derived(!$drawerOpen && pathname === '/scheduler');
  let telemetryActive = $derived(!$drawerOpen && pathname === '/telemetry');
  let memoryActive = $derived(!$drawerOpen && pathname === '/memory');
  let filesActive = $derived(!$drawerOpen && pathname === '/files');
  let settingsActive = $derived(!$drawerOpen && pathname === '/settings');
  let moreActive = $derived($drawerOpen);

  function goConv() {
    showListOnMobile();
    if (pathname !== '/') goto('/');
  }

  function goDestination(path: string) {
    if (pathname !== path) goto(path);
  }

  function openDrawer() {
    drawerOpen.set(true);
  }
</script>

{#if variant === 'rail'}
  <nav
    aria-label="Primary navigation"
    class="flex h-full w-rail shrink-0 flex-col items-stretch gap-1 border-r border-border bg-panel py-3"
  >
    <CaptureFab variant="rail" />
    <div class="mt-2 flex flex-1 flex-col gap-1">
      <NavItem icon={MessageSquare} label="Conv" onclick={goConv} active={convActive} variant="rail" />
      <NavItem icon={ListChecks} label="Tasks" onclick={() => goDestination('/tasks')} active={tasksActive} variant="rail" />
      <NavItem icon={ClipboardList} label="Backlog" onclick={() => goDestination('/backlog')} active={backlogActive} variant="rail" />
      <NavItem icon={Clock} label="Scheduler" onclick={() => goDestination('/scheduler')} active={schedulerActive} variant="rail" />
      <NavItem icon={BarChart3} label="Telemetry" onclick={() => goDestination('/telemetry')} active={telemetryActive} variant="rail" />
      <NavItem icon={Brain} label="Memory" onclick={() => goDestination('/memory')} active={memoryActive} variant="rail" />
      <NavItem icon={Folder} label="Files" onclick={() => goDestination('/files')} active={filesActive} variant="rail" />
      <NavItem icon={Settings} label="Settings" onclick={() => goDestination('/settings')} active={settingsActive} variant="rail" />
    </div>
    <NavItem icon={Menu} label="More" onclick={openDrawer} active={moreActive} variant="rail" />
  </nav>
{:else}
  <!-- Mobile bottom nav: 5 items (Memory/Files/Settings live in the drawer on mobile).
       fixed bottom-0 (instead of a member of the shell's flex-col) is defensive
       against h-dvh quirks on real mobile (Android Chrome, iOS Safari): at
       certain timings after reload/PWA launch the dvh measures larger than the
       visible area, pushing the nav below the fold. fixed guarantees it always
       anchors to the bottom edge. z-30 sits above content but below
       overlays/drawer. pb-safe respects the home indicator/gesture bar. -->
  <nav
    aria-label="Primary navigation"
    class="fixed inset-x-0 bottom-0 z-30 flex h-bottomNav shrink-0 items-stretch border-t border-border bg-panel pb-safe md:hidden"
  >
    <NavItem icon={MessageSquare} label="Conv" onclick={goConv} active={convActive} variant="bottom" />
    <NavItem icon={ListChecks} label="Tasks" onclick={() => goDestination('/tasks')} active={tasksActive} variant="bottom" />
    <NavItem icon={ClipboardList} label="Backlog" onclick={() => goDestination('/backlog')} active={backlogActive} variant="bottom" />
    <NavItem icon={BarChart3} label="Telemetry" onclick={() => goDestination('/telemetry')} active={telemetryActive} variant="bottom" />
    <NavItem icon={Menu} label="More" onclick={openDrawer} active={moreActive} variant="bottom" />
  </nav>
{/if}
