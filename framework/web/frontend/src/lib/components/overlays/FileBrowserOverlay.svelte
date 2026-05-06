<script lang="ts">
  import { onDestroy, untrack } from 'svelte';
  import { ChevronRight, FolderClosed, FileText, Home, RefreshCw, ExternalLink } from 'lucide-svelte';
  import Overlay from './Overlay.svelte';
  import { listFiles, type FileEntry } from '$lib/api';
  import { fmtBytes } from '$lib/services/format';
  import {
    fileBrowserTarget,
    fileViewerTarget,
    openOverlay,
    closeOverlay,
    logEvent
  } from '$lib/stores/ui';
  import { goto } from '$app/navigation';

  const ROOTS = ['company', 'repos', 'agents'] as const;
  type Root = typeof ROOTS[number];

  let currentPath = $state<string>('company');
  let entries: FileEntry[] = $state([]);
  let loading = $state(false);
  let error = $state<string | null>(null);
  let showHidden = $state<boolean>(
    typeof window !== 'undefined' && window.localStorage.getItem('files.showHidden') === '1'
  );

  const currentRoot = $derived<Root>((currentPath.split('/')[0] as Root) ?? 'company');
  const segments = $derived(currentPath.split('/').filter(Boolean));

  // Reage ao target mudar — suporta re-abrir o overlay em paths diferentes
  // sem unmount/mount.
  $effect(() => {
    const t = $fileBrowserTarget;
    if (!t) return;
    const path = t.path;
    untrack(() => {
      if (path === currentPath) return;
      currentPath = path;
      load();
    });
  });

  async function load() {
    loading = true;
    error = null;
    try {
      const r = await listFiles(currentPath, showHidden);
      entries = r.entries;
    } catch (e) {
      error = String(e);
      logEvent(`files: ${e}`, 'err');
    } finally {
      loading = false;
    }
  }

  function toggleHidden() {
    showHidden = !showHidden;
    if (typeof window !== 'undefined') {
      window.localStorage.setItem('files.showHidden', showHidden ? '1' : '0');
    }
    load();
  }

  function navigate(entry: FileEntry) {
    if (entry.is_dir) {
      currentPath = entry.path;
      load();
    } else {
      fileViewerTarget.set({ path: entry.path });
      openOverlay('fileViewer');
    }
  }

  function goRoot(root: Root) {
    if (currentPath === root) return;
    currentPath = root;
    load();
  }

  function goSegment(idx: number) {
    const next = segments.slice(0, idx + 1).join('/');
    if (next === currentPath) return;
    currentPath = next;
    load();
  }

  function openInFilesPage() {
    closeOverlay('fileBrowser');
    goto(`/files?path=${encodeURIComponent(currentPath)}`);
  }

  // Load inicial — caso target ja estivesse setado antes do mount.
  load();

  onDestroy(() => {
    // Limpa target pra reabertura futura funcionar como fresh request.
    fileBrowserTarget.set(null);
  });
</script>

<Overlay id="fileBrowser" title="Files" width="max-w-3xl" fullHeight flush>
  {#snippet actions()}
    <button
      type="button"
      class="inline-flex items-center gap-1 rounded-md border border-border bg-panel2 px-2 py-1 text-xs hover:bg-bg"
      class:bg-accent={showHidden}
      class:text-on-accent={showHidden}
      onclick={toggleHidden}
      aria-pressed={showHidden}
      title="Toggle hidden files"
    >{showHidden ? 'Hidden: on' : 'Hidden: off'}</button>
    <button
      type="button"
      class="inline-flex items-center gap-1 rounded-md border border-border bg-panel2 px-2 py-1 text-xs hover:bg-bg"
      onclick={load}
      title="Refresh"
    ><RefreshCw class="h-3 w-3" /> Refresh</button>
    <button
      type="button"
      class="inline-flex items-center gap-1 rounded-md border border-border bg-panel2 px-2 py-1 text-xs hover:bg-bg"
      onclick={openInFilesPage}
      title="Open in Files page"
    ><ExternalLink class="h-3 w-3" /> Open page</button>
  {/snippet}

  <div class="flex h-full flex-col">
    <div class="flex border-b border-border bg-panel2/50 text-sm">
      {#each ROOTS as root (root)}
        <button
          type="button"
          class="flex-1 min-h-tap px-3 py-2 font-medium capitalize transition-colors"
          class:text-fg={currentRoot === root}
          class:bg-panel={currentRoot === root}
          class:text-muted={currentRoot !== root}
          onclick={() => goRoot(root)}
          aria-pressed={currentRoot === root}
        >
          {root}
        </button>
      {/each}
    </div>

    <div class="flex-1 overflow-y-auto p-3">
      <nav
        class="mb-3 flex flex-wrap items-center gap-1 rounded-md border border-border bg-panel2 px-2 py-1.5 font-mono text-xs"
        aria-label="Path breadcrumb"
      >
        <button
          type="button"
          class="inline-flex items-center gap-1 rounded px-1.5 py-0.5 hover:bg-bg"
          onclick={() => goRoot(currentRoot)}
          title={`Back to ${currentRoot}/`}
        >
          <Home class="h-3 w-3" />
          {currentRoot}
        </button>
        {#each segments.slice(1) as seg, i (i)}
          <ChevronRight class="h-3 w-3 text-muted" aria-hidden="true" />
          <button
            type="button"
            class="rounded px-1.5 py-0.5 hover:bg-bg"
            class:text-muted={i + 1 < segments.length - 1}
            class:font-semibold={i + 1 === segments.length - 1}
            onclick={() => goSegment(i + 1)}
          >
            {seg}
          </button>
        {/each}
      </nav>

      {#if loading}
        <p class="text-xs text-muted">loading…</p>
      {:else if error}
        <p class="text-xs text-accent2">error: {error}</p>
      {:else if !entries.length}
        <p class="text-xs text-muted">empty</p>
      {:else}
        <div class="grid gap-1">
          {#each entries as e (e.path)}
            <button
              type="button"
              class="flex min-h-tap items-center gap-2 rounded-md border border-border bg-panel2 px-2 py-2 text-left text-sm hover:bg-bg"
              onclick={() => navigate(e)}
            >
              {#if e.is_dir}
                <FolderClosed class="h-4 w-4 text-accent" />
              {:else}
                <FileText class="h-4 w-4 text-muted" />
              {/if}
              <span class="flex-1 truncate">{e.name}</span>
              <span class="text-xs text-muted">
                {e.is_dir ? '' : fmtBytes(e.size)}
              </span>
            </button>
          {/each}
        </div>
      {/if}
    </div>
  </div>
</Overlay>
