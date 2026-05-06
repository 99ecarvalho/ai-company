<script lang="ts">
  import { onDestroy, untrack } from 'svelte';
  import { Download, Pencil, Save, X } from 'lucide-svelte';
  import Overlay from './Overlay.svelte';
  import {
    fileDownloadUrl,
    readFile,
    writeFile,
    type FileReadResult
  } from '$lib/api';
  import { renderMarkdownFile } from '$lib/services/markdown';
  import { fileViewerTarget, logEvent } from '$lib/stores/ui';

  let result: FileReadResult | null = $state(null);
  let loading = $state(false);
  let error = $state<string | null>(null);
  let currentPath = $state<string>('');

  // Edit mode
  let editing = $state(false);
  let editContent = $state('');
  let saving = $state(false);
  let saveError = $state<string | null>(null);

  // Code highlight (lazy)
  let highlightedHtml = $state<string | null>(null);

  $effect(() => {
    const target = $fileViewerTarget;
    if (!target) return;
    const path = target.path;
    untrack(() => {
      if (path === currentPath && !editing) return;
      currentPath = path;
      editing = false;
      saveError = null;
      load(path);
    });
  });

  async function load(path: string) {
    loading = true;
    error = null;
    highlightedHtml = null;
    if (result?.blobUrl) {
      try { URL.revokeObjectURL(result.blobUrl); } catch { /* noop */ }
    }
    result = null;
    try {
      const r = await readFile(path);
      result = r;
      if (r.kind === 'text' && r.text != null && !isMarkdown(path)) {
        applyHighlight(path, r.text);
      }
    } catch (e) {
      error = String(e);
    } finally {
      loading = false;
    }
  }

  function isMarkdown(path: string): boolean {
    return path.toLowerCase().endsWith('.md');
  }

  function isWritable(path: string): boolean {
    const head = path.split('/')[0];
    return head === 'company' || head === 'agents';
  }

  // Map de extensoes simples -> language pra hljs (alias dos suportados).
  const EXT_LANG: Record<string, string> = {
    py: 'python', js: 'javascript', ts: 'typescript', tsx: 'typescript',
    jsx: 'javascript', mjs: 'javascript', cjs: 'javascript',
    svelte: 'xml', vue: 'xml', html: 'xml', xml: 'xml',
    css: 'css', scss: 'scss', less: 'less',
    json: 'json', yaml: 'yaml', yml: 'yaml', toml: 'ini', ini: 'ini',
    sh: 'bash', bash: 'bash', zsh: 'bash',
    sql: 'sql', go: 'go', rs: 'rust', rb: 'ruby', php: 'php',
    java: 'java', kt: 'kotlin', swift: 'swift',
    c: 'c', cpp: 'cpp', h: 'c', hpp: 'cpp',
    log: 'plaintext', txt: 'plaintext', csv: 'plaintext'
  };

  async function applyHighlight(path: string, text: string): Promise<void> {
    const ext = (path.split('.').pop() || '').toLowerCase();
    const lang = EXT_LANG[ext];
    if (!lang || lang === 'plaintext') return;
    try {
      const hljs = (await import('highlight.js/lib/core')).default;
      const langFn = await loadLang(lang);
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      if (langFn) hljs.registerLanguage(lang, langFn as any);
      ensureHighlightStyle();
      const out = hljs.highlight(text, { language: lang, ignoreIllegals: true });
      highlightedHtml = out.value;
    } catch {
      highlightedHtml = null;
    }
  }

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  async function loadLang(lang: string): Promise<any | null> {
    try {
      const mod = await import(`highlight.js/lib/languages/${lang}.js`);
      return mod.default || mod;
    } catch {
      return null;
    }
  }

  let styleEnsured = false;
  function ensureHighlightStyle() {
    if (styleEnsured || typeof document === 'undefined') return;
    styleEnsured = true;
    import('highlight.js/styles/github-dark.css');
  }

  function startEdit() {
    if (!result || result.kind !== 'text' || result.text == null) return;
    editContent = result.text;
    saveError = null;
    editing = true;
  }

  function cancelEdit() {
    editing = false;
    saveError = null;
  }

  async function save() {
    if (!currentPath) return;
    saving = true;
    saveError = null;
    try {
      await writeFile(currentPath, editContent);
      logEvent(`saved: ${currentPath}`, 'ok');
      editing = false;
      // re-load pra refletir o novo conteudo (e re-highlight)
      await load(currentPath);
    } catch (e) {
      saveError = String(e);
      logEvent(`save: ${e}`, 'err');
    } finally {
      saving = false;
    }
  }

  onDestroy(() => {
    if (result?.blobUrl) {
      try { URL.revokeObjectURL(result.blobUrl); } catch { /* noop */ }
    }
  });
</script>

<Overlay id="fileViewer" title={currentPath || 'File viewer'} width="max-w-4xl">
  {#snippet actions()}
    {#if currentPath && !editing}
      {#if result?.kind === 'text' && isWritable(currentPath)}
        <button
          type="button"
          onclick={startEdit}
          class="inline-flex items-center gap-1 rounded-md border border-border bg-panel2 px-2 py-1 text-xs hover:bg-bg"
          title="Edit"
        >
          <Pencil class="h-3 w-3" /> Edit
        </button>
      {/if}
      <a
        href={fileDownloadUrl(currentPath)}
        download
        class="inline-flex items-center gap-1 rounded-md border border-border bg-panel2 px-2 py-1 text-xs hover:bg-bg"
        title="Download"
      >
        <Download class="h-3 w-3" /> Download
      </a>
    {/if}
    {#if editing}
      <button
        type="button"
        onclick={save}
        disabled={saving}
        class="inline-flex items-center gap-1 rounded-md bg-accent px-2 py-1 text-xs font-semibold text-on-accent hover:bg-accent/90 disabled:opacity-50"
      >
        <Save class="h-3 w-3" /> {saving ? 'Saving…' : 'Save'}
      </button>
      <button
        type="button"
        onclick={cancelEdit}
        disabled={saving}
        class="inline-flex items-center gap-1 rounded-md border border-border bg-panel2 px-2 py-1 text-xs hover:bg-bg"
      >
        <X class="h-3 w-3" /> Cancel
      </button>
    {/if}
  {/snippet}

  {#if loading}
    <p class="text-xs text-muted">loading…</p>
  {:else if error}
    <p class="text-xs text-accent2">error: {error}</p>
  {:else if editing}
    {#if saveError}
      <p class="mb-2 text-xs text-accent2">save error: {saveError}</p>
    {/if}
    <textarea
      bind:value={editContent}
      class="h-[60vh] w-full resize-none rounded-md border border-border bg-bg p-3 font-mono text-[12px] leading-relaxed focus:border-accent focus:outline-none"
      spellcheck="false"
    ></textarea>
  {:else if result}
    {#if result.kind === 'text' && isMarkdown(result.path) && result.text}
      <div class="mdBody text-sm leading-relaxed">
        {@html renderMarkdownFile(result.text)}
      </div>
    {:else if result.kind === 'text' && result.text != null}
      <pre class="overflow-x-auto rounded-md bg-bg p-3 font-mono text-[12px] leading-relaxed"><code class="hljs">{#if highlightedHtml}{@html highlightedHtml}{:else}{result.text}{/if}</code></pre>
    {:else if result.kind === 'image' && result.blobUrl}
      <img src={result.blobUrl} alt={result.path} class="max-w-full rounded-md" />
    {:else}
      <p class="text-xs text-muted">binary file ({result.mime}) — use download</p>
    {/if}
  {/if}
</Overlay>
