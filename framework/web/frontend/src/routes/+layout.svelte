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

  // Rotas que nao usam o shell (rail/list/bottom-nav). Login e onboarding
  // tem fluxo proprio centralizado.
  const bareRoutes = new Set(['/login', '/onboard']);
  let isBareRoute = $derived(bareRoutes.has($page.url.pathname));

  // Home = "/" (conv list/capture). Em rotas nao-home, ConvListPane fica
  // escondida no mobile (bottom-nav devolve pra home).
  let isHomeRoute = $derived($page.url.pathname === '/');

  // ConvListPane aparece so nas rotas de conversas (/ e /c/*). Outras rotas
  // (tasks/scheduler/telemetry/etc) usam o espaco inteiro pra seu conteudo.
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

    // Deep link ?ask=<id> — mesmo comportamento que tinha em +page.svelte
    // antes da reestruturacao; agora mora no layout pra funcionar em
    // qualquer rota de boot.
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
    <!-- pb-safe-nav reserva `bottom-nav-h + env(safe-area-inset-bottom)`
         pro bottom nav (`fixed`, fora do flex flow) + gesture bar do OS
         (Android nav bar / iOS home indicator). Antes usava `pb-bottomNav`
         (so 56px), conteudo com scroll ficava cortado em devices com
         gesture bar > 0. md:pb-0 anula em desktop (sem bottom nav). -->
    <div class="flex min-h-0 flex-1 overflow-hidden pb-safe-nav md:pb-0">
      <!-- Desktop rail -->
      <div class="hidden md:flex">
        <AppNav variant="rail" />
      </div>

      <!-- ConvListPane: so nas rotas de conversas (/ e /c/*). Desktop
           sempre visivel quando isConvRoute; mobile so quando isHomeRoute
           e mobileShowList. -->
      {#if isConvRoute}
        <!-- Mobile: full-width + min-w-0 (sem isso, conteudo do aside vaza
             alem do viewport). Desktop: largura definida pelo aside (w-sidebar). -->
        <div
          class="flex h-full w-full min-w-0 md:w-auto md:flex"
          class:hidden={!(isHomeRoute && $mobileShowList)}
        >
          <ConvListPane />
        </div>
      {/if}

      <!-- Main: sempre visivel desktop; mobile so quando nao for home-list. -->
      <main
        class="min-w-0 flex-1 md:block"
        class:hidden={isHomeRoute && $mobileShowList}
      >
        {@render children()}
      </main>
    </div>

    <!-- Mobile bottom nav -->
    <AppNav variant="bottom" />

    <!-- FAB: somente mobile e somente em home-list (estado "lista vista"). -->
    {#if isHomeRoute && $mobileShowList}
      <CaptureFab variant="fab" />
    {/if}

    <!-- Overlays residuais (hire, fileViewer) -->
    {#each Array.from($openOverlays) as id (id)}
      {@const Cmp = overlays[id]}
      {#if Cmp}<Cmp />{/if}
    {/each}

    <!-- Drawer lateral -->
    <DrawerMenu />
  </div>
{/if}

<!-- Toaster fica fora do {#if} pra cobrir rotas bare (login/onboard) e o
     período pré-boot — qualquer logEvent('err') ganha feedback visual. -->
<Toaster />
