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

  // Cast: SvelteKit tipa pathname como uniao restrita das rotas conhecidas.
  let pathname = $derived($page.url.pathname as string);

  // Active-tab: drawer em primeiro lugar; depois matching por pathname.
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
  <!-- Mobile bottom nav: 5 itens (Memory/Files/Settings ficam no drawer no mobile).
       fixed bottom-0 (em vez de membro do flex-col do shell) eh defensivo
       contra quirks de h-dvh em mobile real (Android Chrome, iOS Safari): em
       certos timings pos-reload/pos-PWA-launch o dvh mede maior que o
       visivel, empurrando o nav embaixo da fold. fixed garante que sempre
       ancora na borda inferior. z-30 fica acima do conteudo mas abaixo de
       overlays/drawer. pb-safe respeita home indicator/gesture bar. -->
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
