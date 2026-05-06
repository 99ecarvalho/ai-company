<script lang="ts">
  import { onMount } from 'svelte';
  import { Pencil, Trash2, Check, X } from 'lucide-svelte';
  import PageHeader from '$lib/components/ui/PageHeader.svelte';
  import {
    listMemoryAgents,
    listMemoryFacts,
    saveMemoryFact,
    updateMemoryFact,
    deleteMemoryFact,
    type MemoryAgent,
    type MemoryFact
  } from '$lib/api';
  import { logEvent } from '$lib/stores/ui';

  let agents: MemoryAgent[] = $state([]);
  let agent = $state<string>('');
  let q = $state('');
  let tagFilter = $state('');
  let facts: MemoryFact[] = $state([]);
  let total = $state(0);
  let loading = $state(false);
  let error = $state<string | null>(null);

  // Add form
  let newKey = $state('');
  let newValue = $state('');
  let newTags = $state('');

  // Edit inline state
  let editingKey = $state<string | null>(null);
  let editValue = $state('');
  let editTags = $state('');
  let editSaving = $state(false);

  let searchTimer: ReturnType<typeof setTimeout> | null = null;

  async function loadAgents() {
    try {
      const { items } = await listMemoryAgents();
      agents = items;
      if (!agent && items.length) agent = items[0].agent;
      if (agent) await loadFacts();
    } catch (e) {
      error = String(e);
    }
  }

  async function loadFacts() {
    if (!agent) return;
    loading = true;
    error = null;
    try {
      const { items, count } = await listMemoryFacts(
        agent,
        q.trim() || undefined,
        tagFilter.trim() || undefined
      );
      facts = items;
      total = count;
    } catch (e) {
      error = String(e);
    } finally {
      loading = false;
    }
  }

  function onSearchInput() {
    if (searchTimer) clearTimeout(searchTimer);
    searchTimer = setTimeout(loadFacts, 300);
  }

  async function onAdd() {
    if (!newKey.trim() || !newValue.trim() || !agent) return;
    const tags = newTags.split(',').map((t) => t.trim()).filter(Boolean);
    try {
      await saveMemoryFact(agent, {
        key: newKey.trim(),
        value: newValue.trim(),
        tags
      });
      logEvent(`memory: saved ${newKey} -> ${agent}`, 'ok');
      newKey = '';
      newValue = '';
      newTags = '';
      await loadAgents();
    } catch (e) {
      logEvent(`memory save: ${e}`, 'err');
    }
  }

  function startEdit(f: MemoryFact) {
    editingKey = f.key;
    editValue = f.value;
    editTags = f.tags.join(', ');
  }

  function cancelEdit() {
    editingKey = null;
    editValue = '';
    editTags = '';
  }

  async function saveEdit() {
    if (editingKey === null || !agent) return;
    editSaving = true;
    const tags = editTags.split(',').map((t) => t.trim()).filter(Boolean);
    try {
      await updateMemoryFact(agent, editingKey, {
        value: editValue,
        tags
      });
      logEvent(`memory: edited ${editingKey} in ${agent}`, 'ok');
      editingKey = null;
      await loadFacts();
    } catch (e) {
      logEvent(`memory edit: ${e}`, 'err');
    } finally {
      editSaving = false;
    }
  }

  async function onDelete(key: string) {
    if (!confirm(`Delete "${key}" from ${agent}'s memory?`)) return;
    try {
      await deleteMemoryFact(agent, key);
      logEvent(`memory: removed ${key} from ${agent}`, 'ok');
      await loadAgents();
    } catch (e) {
      logEvent(`memory delete: ${e}`, 'err');
    }
  }

  onMount(loadAgents);
</script>

<div class="flex h-full flex-col overflow-hidden">
  <PageHeader title="Memory">
    {#snippet actions()}
      <button
        type="button"
        class="min-h-tap rounded-md border border-border bg-panel2 px-3 py-1.5 text-sm hover:bg-bg"
        onclick={loadAgents}
      >Refresh</button>
    {/snippet}
  </PageHeader>

  <div class="flex-1 overflow-y-auto overflow-x-hidden p-4">
  <!-- Mobile: stack vertical (cada filtro full-width). sm+: row inline. -->
  <div class="mb-3 flex flex-col gap-2 sm:flex-row sm:flex-wrap sm:items-center">
    <select
      class="w-full rounded-md border border-border bg-panel2 px-2 py-1 text-xs sm:w-auto"
      aria-label="Agent"
      bind:value={agent}
      onchange={loadFacts}
    >
      {#each agents as a (a.agent)}
        <option value={a.agent}>{a.agent} ({a.count})</option>
      {/each}
    </select>
    <input
      type="text"
      class="w-full rounded-md border border-border bg-panel2 px-2 py-1 text-xs sm:min-w-[160px] sm:flex-1"
      placeholder="Search facts…"
      aria-label="Search facts"
      bind:value={q}
      oninput={onSearchInput}
    />
    <input
      type="text"
      class="w-full rounded-md border border-border bg-panel2 px-2 py-1 text-xs sm:w-32"
      placeholder="filter tag"
      aria-label="Filter by tag"
      bind:value={tagFilter}
      oninput={onSearchInput}
    />
  </div>

  {#if loading}
    <p class="text-xs text-muted">loading…</p>
  {:else if error}
    <p class="text-xs text-accent2">error: {error}</p>
  {:else if !facts.length}
    <p class="text-xs text-muted">{q || tagFilter ? 'no match' : 'memory empty'} (total: {total})</p>
  {:else}
    <ul class="flex flex-col gap-2">
      {#each facts as f (f.id)}
        <li class="rounded-md border border-border bg-panel2 p-2.5" data-fact-key={f.key}>
          {#if editingKey === f.key}
            <div class="flex flex-col gap-2">
              <div class="flex items-baseline gap-2">
                <strong class="text-xs">{f.key}</strong>
                <span class="text-[10px] text-muted">editing</span>
              </div>
              <textarea
                rows="3"
                class="rounded-md border border-border bg-bg px-2 py-1 text-xs"
                aria-label="Edit value"
                bind:value={editValue}
              ></textarea>
              <input
                class="rounded-md border border-border bg-bg px-2 py-1 text-xs"
                placeholder="tags (comma-separated)"
                aria-label="Edit tags"
                bind:value={editTags}
              />
              <div class="flex gap-2">
                <button
                  type="button"
                  class="inline-flex items-center gap-1 rounded-md bg-accent px-2 py-1 text-xs font-semibold text-on-accent hover:bg-accent/90 disabled:opacity-50"
                  disabled={editSaving || !editValue.trim()}
                  onclick={saveEdit}
                >
                  <Check class="h-3 w-3" />
                  Save
                </button>
                <button
                  type="button"
                  class="inline-flex items-center gap-1 rounded-md border border-border bg-panel2 px-2 py-1 text-xs hover:bg-bg"
                  disabled={editSaving}
                  onclick={cancelEdit}
                >
                  <X class="h-3 w-3" />
                  Cancel
                </button>
              </div>
            </div>
          {:else}
            <div class="flex items-start justify-between gap-2">
              <div class="flex-1 min-w-0">
                <div class="flex flex-wrap items-baseline gap-2">
                  <strong class="text-xs">{f.key}</strong>
                  {#if f.tags?.length}
                    <span class="text-[10px] text-muted">{f.tags.join(', ')}</span>
                  {/if}
                </div>
                <div class="mt-1 whitespace-pre-wrap text-xs text-fg/90">{f.value}</div>
                <div class="mt-1 text-[10px] text-muted">
                  id {f.id} · updated {new Date(f.updated_at * 1000).toLocaleString()}
                </div>
              </div>
              <div class="flex flex-col gap-1">
                <button
                  type="button"
                  class="rounded p-1 text-muted hover:bg-bg hover:text-accent"
                  aria-label="Edit fact"
                  title="Edit"
                  onclick={() => startEdit(f)}
                >
                  <Pencil class="h-3.5 w-3.5" />
                </button>
                <button
                  type="button"
                  class="rounded p-1 text-muted hover:bg-bg hover:text-accent2"
                  aria-label="Delete fact"
                  title="Delete"
                  onclick={() => onDelete(f.key)}
                >
                  <Trash2 class="h-3.5 w-3.5" />
                </button>
              </div>
            </div>
          {/if}
        </li>
      {/each}
    </ul>
  {/if}

  <div class="mt-4 border-t border-border pt-3">
    <div class="mb-2 text-xs font-semibold text-muted">Add fact</div>
    <div class="flex flex-col gap-2">
      <input
        class="rounded-md border border-border bg-panel2 px-2 py-1 text-xs"
        placeholder="key"
        bind:value={newKey}
      />
      <textarea
        rows="3"
        class="rounded-md border border-border bg-panel2 px-2 py-1 text-xs"
        placeholder="value"
        bind:value={newValue}
      ></textarea>
      <input
        class="rounded-md border border-border bg-panel2 px-2 py-1 text-xs"
        placeholder="tags (comma-separated)"
        bind:value={newTags}
      />
      <button
        type="button"
        class="self-start rounded-md bg-accent px-3 py-1.5 text-xs font-semibold text-on-accent hover:bg-accent/90 disabled:opacity-50"
        disabled={!newKey.trim() || !newValue.trim() || !agent}
        onclick={onAdd}
      >Save</button>
    </div>
  </div>
  </div>
</div>
