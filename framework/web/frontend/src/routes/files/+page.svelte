<script lang="ts">
  import { onMount } from 'svelte';
  import { ChevronRight, FolderClosed, FileText, Home } from 'lucide-svelte';
  import { page } from '$app/stores';
  import PageHeader from '$lib/components/ui/PageHeader.svelte';
  import { listFiles, type FileEntry } from '$lib/api';
  import { fmtBytes } from '$lib/services/format';
  import {
    fileViewerTarget,
    openOverlay,
    logEvent
  } from '$lib/stores/ui';

  // Tres raizes que o backend aceita (files.py: COMPANY_DIR/REPOS_DIR/AGENTS_DIR).
  const ROOTS = ['company', 'repos', 'agents'] as const;
  type Root = typeof ROOTS[number];

  // Deep-link via query param `?path=...`. Default: company.
  let path = $state<string>('company');
  let entries: FileEntry[] = $state([]);
  let loading = $state(false);
  let error = $state<string | null>(null);
  // Dotfiles ocultas por default (.git/, .DS_Store, .cache/ etc poluem
  // mas as vezes precisa ver — toggle persistente em localStorage).
  let showHidden = $state<boolean>(
    typeof window !== 'undefined' && window.localStorage.getItem('files.showHidden') === '1'
  );

  // Raiz atual = primeiro segmento; breadcrumb = segmentos restantes.
  const currentRoot = $derived<Root>((path.split('/')[0] as Root) ?? 'company');
  const segments = $derived(path.split('/').filter(Boolean));

  async function load() {
    loading = true;
    error = null;
    try {
      const r = await listFiles(path, showHidden);
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
      path = entry.path;
      load();
    } else {
      fileViewerTarget.set({ path: entry.path });
      openOverlay('fileViewer');
    }
  }

  function goRoot(root: Root) {
    if (path === root) return;
    path = root;
    load();
  }

  function goSegment(idx: number) {
    // Click num breadcrumb: navega ate aquele segmento (incluso).
    const next = segments.slice(0, idx + 1).join('/');
    if (next === path) return;
    path = next;
    load();
  }

  onMount(() => {
    const q = $page.url.searchParams.get('path');
    if (q) path = q;
    load();
  });
</script>

<div class="flex h-full flex-col overflow-hidden">
  <PageHeader title="Files">
    {#snippet actions()}
      <button
        type="button"
        class="min-h-tap rounded-md border border-border px-3 py-1.5 text-sm hover:bg-bg"
        class:bg-accent={showHidden}
        class:text-on-accent={showHidden}
        class:bg-panel2={!showHidden}
        onclick={toggleHidden}
        aria-pressed={showHidden}
        title="Toggle hidden files (.git, .DS_Store, ...)"
      >{showHidden ? 'Hidden: on' : 'Hidden: off'}</button>
      <button
        type="button"
        class="min-h-tap rounded-md border border-border bg-panel2 px-3 py-1.5 text-sm hover:bg-bg"
        onclick={load}
      >Refresh</button>
    {/snippet}
  </PageHeader>

  <!-- Root selector: Company / Repos / Agents. Substitui o botao "Up"
       que ciclava entre as duas primeiras e nem expunha agents. -->
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
        title={`Browse ${root}/`}
      >
        {root}
      </button>
    {/each}
  </div>

  <div class="flex-1 overflow-y-auto p-4">
    <!-- Breadcrumb clicavel: cada segmento navega de volta ao seu nivel. -->
    <nav class="mb-3 flex flex-wrap items-center gap-1 rounded-md border border-border bg-panel2 px-2 py-1.5 font-mono text-xs"
         aria-label="Path breadcrumb">
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
