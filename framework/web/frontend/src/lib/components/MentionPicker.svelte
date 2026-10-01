<script lang="ts">
  /**
   * @-mention picker attached to a textarea. Parent wires it in by:
   *   1. Binding the textarea element.
   *   2. Calling `onTextareaKeydown(e)` from the textarea's on:keydown.
   *   3. Calling `onTextareaInput()` from the textarea's on:input.
   *
   * When the user types `@`, we track everything after the `@` until
   * whitespace/newline/end as the query. Results come from /api/files/search.
   * On select, we replace `@<query>` with `<prefixed path> ` (trailing space)
   * so the chat markdown renderer (`linkifyFilePaths`) turns it clickable and
   * the agent can `Read()` the absolute path directly.
   */
  import { onDestroy } from 'svelte';
  import { Folder, FileText } from 'lucide-svelte';
  import { searchFiles, type FileSearchEntry } from '$lib/api';

  interface Props {
    textarea: HTMLTextAreaElement | null;
    value: string;
    onUpdate: (v: string) => void;
    /** Prefix prepended to the inserted path. Default `/workspace/` so the
     *  agent can feed it to `Read()` unchanged. */
    pathPrefix?: string;
  }
  let {
    textarea,
    value = $bindable(),
    onUpdate,
    pathPrefix = '/workspace/',
  }: Props = $props();

  // Picker state. `active` becomes true when we detect `@xxx` at the caret
  // position; `at` and `caret` delimit the range to replace on select.
  let active = $state(false);
  let at = $state<number>(-1);
  let caret = $state<number>(-1);
  let query = $state('');
  let entries = $state<FileSearchEntry[]>([]);
  let highlighted = $state(0);
  let loading = $state(false);

  // The @... token starts at an `@` preceded by start/whitespace and runs to
  // the next whitespace or end. Supports all chars valid in paths.
  const AT_RE = /(^|\s)@([a-zA-Z0-9._/\-]*)$/;

  function detectMention() {
    if (!textarea) {
      active = false;
      return;
    }
    const pos = textarea.selectionStart ?? 0;
    const before = value.slice(0, pos);
    const m = before.match(AT_RE);
    if (!m) {
      active = false;
      return;
    }
    active = true;
    at = pos - m[2].length - 1; // position of the `@`
    caret = pos;
    query = m[2];
    highlighted = 0;
    triggerSearch();
  }

  let searchToken = 0;
  async function triggerSearch() {
    const token = ++searchToken;
    loading = true;
    try {
      const r = await searchFiles(query, { limit: 20 });
      if (token !== searchToken) return; // a newer search won
      entries = r.entries;
      if (highlighted >= entries.length) highlighted = 0;
    } catch {
      entries = [];
    } finally {
      if (token === searchToken) loading = false;
    }
  }

  export function onTextareaInput() {
    detectMention();
  }

  export function onTextareaKeydown(e: KeyboardEvent): boolean {
    // Returns true if it consumed the event (parent should stop propagation).
    if (!active) return false;
    if (e.key === 'Escape') {
      active = false;
      e.preventDefault();
      return true;
    }
    if (e.key === 'ArrowDown') {
      highlighted = Math.min(entries.length - 1, highlighted + 1);
      e.preventDefault();
      return true;
    }
    if (e.key === 'ArrowUp') {
      highlighted = Math.max(0, highlighted - 1);
      e.preventDefault();
      return true;
    }
    if ((e.key === 'Enter' || e.key === 'Tab') && entries.length > 0) {
      e.preventDefault();
      select(entries[highlighted]);
      return true;
    }
    return false;
  }

  function select(entry: FileSearchEntry) {
    if (at < 0) return;
    const inserted = `${pathPrefix}${entry.path}${entry.is_dir ? '/' : ''} `;
    const next = value.slice(0, at) + inserted + value.slice(caret);
    onUpdate(next);
    const newCaret = at + inserted.length;
    active = false;
    // Re-position the caret after the insert (waits a tick for the DOM to update).
    queueMicrotask(() => {
      if (textarea) {
        textarea.focus();
        textarea.setSelectionRange(newCaret, newCaret);
      }
    });
  }

  function onOutsideClick(e: MouseEvent) {
    if (!active) return;
    const t = e.target as Node | null;
    if (t && pickerEl && !pickerEl.contains(t) && t !== textarea) {
      active = false;
    }
  }

  let pickerEl = $state<HTMLDivElement | null>(null);
  $effect(() => {
    if (active) {
      document.addEventListener('mousedown', onOutsideClick);
      return () => document.removeEventListener('mousedown', onOutsideClick);
    }
  });
  onDestroy(() => document.removeEventListener('mousedown', onOutsideClick));
</script>

{#if active}
  <div
    bind:this={pickerEl}
    class="absolute bottom-full left-0 right-0 z-20 mb-1 max-h-[260px] overflow-y-auto rounded-md border border-border bg-panel2 shadow-lg"
  >
    <div class="sticky top-0 z-10 border-b border-border bg-panel2 px-2 py-1 text-[10px] uppercase tracking-wider text-muted">
      {loading ? 'searching…' : entries.length === 0 ? 'no matches' : `${entries.length} match${entries.length === 1 ? '' : 'es'}`}
      {#if query}<span class="ml-1 normal-case text-fg/70">· @{query}</span>{/if}
    </div>
    {#each entries as e, i (e.path)}
      <button
        type="button"
        class="flex w-full items-center gap-2 px-2 py-1 text-left text-[12px] {i === highlighted ? 'bg-accent text-on-accent' : 'hover:bg-bg'}"
        onmousedown={(ev) => { ev.preventDefault(); select(e); }}
        onmouseenter={() => (highlighted = i)}
      >
        {#if e.is_dir}
          <Folder class="h-3 w-3 shrink-0 {i === highlighted ? 'text-on-accent' : 'text-warn'}" />
        {:else}
          <FileText class="h-3 w-3 shrink-0 {i === highlighted ? 'text-on-accent' : 'text-muted'}" />
        {/if}
        <span class="truncate font-mono {i === highlighted ? 'text-on-accent' : 'text-fg'}">{e.path}{e.is_dir ? '/' : ''}</span>
      </button>
    {/each}
  </div>
{/if}
