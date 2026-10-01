<script lang="ts">
  import '../app.css';
  import { onMount, onDestroy } from 'svelte';
  import { goto } from '$app/navigation';
  import { page } from '$app/stores';
  import {
    startConversationsStream,
    stopConversationsStream
  } from '$lib/stores/conversations';
  import { startTasksStream, stopTasksStream } from '$lib/stores/tasks';
  import { refreshStreams } from '$lib/stores/streams';
  import { refreshPushState } from '$lib/stores/push';
  import { registerServiceWorker } from '$lib/services/push';
  import { logEvent, openOverlays, mobileShowList } from '$lib/stores/ui';
  import { refreshAuth } from '$lib/stores/auth';
  import { theme, applyTheme } from '$lib/stores/theme';
  import { onboardStatus, listPendingAsks } from '$lib/api';
  import AppNav from '$lib/components/nav/AppNav.svelte';
  import CaptureFab from '$lib/components/nav/CaptureFab.svelte';
  import DrawerMenu from '$lib/components/nav/DrawerMenu.svelte';
  import ConvListPane from '$lib/components/ConvListPane.svelte';
  import Toaster from '$lib/components/Toaster.svelte';
  import { overlays } from '$lib/config/overlays';
  import { showConvPanel } from '$lib/stores/ui';

  let { children } = $props();
  let booted = $state(false);

  // Routes that don't use the shell (rail/list/bottom-nav). Login and onboarding
  // have their own centered flow.
  const bareRoutes = new Set(['/login', '/onboard']);
  let isBareRoute = $derived(bareRoutes.has($page.url.pathname));

  // Home = "/" (conv list/capture). On non-home routes, ConvListPane is
  // hidden on mobile (bottom-nav takes you back home).
  let isHomeRoute = $derived($page.url.pathname === '/');

  // ConvListPane only appears on conversation routes (/ and /c/*). Other routes
  // (tasks/scheduler/telemetry/etc) use the whole space for their content.
  let isConvRoute = $derived(
    $page.url.pathname === '/' || $page.url.pathname.startsWith('/c/')
  );

  onMount(async () => {
    applyTheme($theme);
    const me = await refreshAuth();
    if (!me) {
      if ($page.url.pathname !== '/login') {
        goto('/login', { replaceState: true });
      }
      booted = true;
      return;
    }
    if (String($page.url.pathname) !== '/onboard' && $page.url.pathname !== '/login') {
      try {
        const status = await onboardStatus();
        if (status.fresh) {
          goto('/onboard', { replaceState: true });
          booted = true;
          return;
        }
      } catch {
        /* ignore */
      }
    }
    const reg = await registerServiceWorker();
    if (reg) logEvent('sw registered', 'ok');
    else logEvent('sw unavailable', '');
    refreshStreams();
    refreshPushState();
    startConversationsStream();
    startTasksStream();

    // Deep link ?ask=<id> — same behavior it had in +page.svelte
    // before the restructuring; now it lives in the layout so it works on
    // any boot route.
    const askId = $page.url.searchParams.get('ask');
    if (askId) {
      try {
        const { items } = await listPendingAsks();
        const a = items.find((x) => x.id === askId);
        if (a) showConvPanel(`${a.stream}/${a.topic}`);
      } catch {
        /* best-effort */
      }
    }

    booted = true;
  });

  onDestroy(() => {
    stopConversationsStream();
    stopTasksStream();
  });
</script>

{#if isBareRoute}
  {@render children()}
{:else if booted}
  <div class="flex h-dvh w-screen flex-col overflow-hidden bg-bg text-fg">
    <!-- pb-safe-nav reserves `bottom-nav-h + env(safe-area-inset-bottom)`
         for the bottom nav (`fixed`, outside the flex flow) + the OS gesture bar
         (Android nav bar / iOS home indicator). It used to be `pb-bottomNav`
         (only 56px); scrolling content got cut off on devices with
         gesture bar > 0. md:pb-0 cancels it on desktop (no bottom nav). -->
    <div class="flex min-h-0 flex-1 overflow-hidden pb-safe-nav md:pb-0">
      <!-- Desktop rail -->
      <div class="hidden md:flex">
        <AppNav variant="rail" />
      </div>

      <!-- ConvListPane: only on conversation routes (/ and /c/*). Desktop:
           always visible when isConvRoute; mobile: only when isHomeRoute
           and mobileShowList. -->
      {#if isConvRoute}
        <!-- Mobile: full-width + min-w-0 (without it, the aside's content leaks
             past the viewport). Desktop: width set by the aside (w-sidebar). -->
        <div
          class="flex h-full w-full min-w-0 md:w-auto md:flex"
          class:hidden={!(isHomeRoute && $mobileShowList)}
        >
          <ConvListPane />
        </div>
      {/if}

      <!-- Main: always visible on desktop; on mobile only when not on home-list. -->
      <main
        class="min-w-0 flex-1 md:block"
        class:hidden={isHomeRoute && $mobileShowList}
      >
        {@render children()}
      </main>
    </div>

    <!-- Mobile bottom nav -->
    <AppNav variant="bottom" />

    <!-- FAB: mobile only and only on home-list (the "list shown" state). -->
    {#if isHomeRoute && $mobileShowList}
      <CaptureFab variant="fab" />
    {/if}

    <!-- Residual overlays (hire, fileViewer) -->
    {#each Array.from($openOverlays) as id (id)}
      {@const Cmp = overlays[id]}
      {#if Cmp}<Cmp />{/if}
    {/each}

    <!-- Side drawer -->
    <DrawerMenu />
  </div>
{/if}

<!-- Toaster stays outside the {#if} to cover bare routes (login/onboard) and the
     pre-boot period — any logEvent('err') gets visual feedback. -->
<Toaster />
