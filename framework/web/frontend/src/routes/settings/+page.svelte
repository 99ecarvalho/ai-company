<script lang="ts">
  import { onMount } from 'svelte';
  import { ScrollText, Eye, Save, RefreshCw, Users, ArrowLeft, Clock, Workflow, Settings as SettingsIcon } from 'lucide-svelte';
  import { page } from '$app/stores';
  import PageHeader from '$lib/components/ui/PageHeader.svelte';
  import RoutinesPanel from '$lib/components/settings/RoutinesPanel.svelte';
  import WorkflowsPanel from '$lib/components/settings/WorkflowsPanel.svelte';
  import SystemPanel from '$lib/components/settings/SystemPanel.svelte';
  import {
    getSystemPromptsIndex,
    getSystemPromptSection,
    setSystemPromptSection,
    setSystemPromptConfig,
    getSystemPromptPreview,
    listAgentsMeta,
    listAgentPolicies,
    upsertAgentPolicy,
    type SystemPromptIndex,
    type SystemPromptToggleKey,
    type AgentMeta
  } from '$lib/api';
  import { logEvent } from '$lib/stores/ui';

  type Tab = 'sections' | 'preview' | 'agents' | 'routines' | 'workflows' | 'system';
  // Tab inicial via ?tab= (deep-link de outras paginas, ex: /scheduler).
  const initialTab = (() => {
    const t = $page.url.searchParams.get('tab');
    return t === 'preview' || t === 'agents' || t === 'routines' || t === 'workflows' || t === 'system'
      ? t
      : 'sections';
  })();
  let tab = $state<Tab>(initialTab);

  // ---------- Sections / Preview state ----------
  let index = $state<SystemPromptIndex | null>(null);
  let activeKey = $state<string | null>(null); // null = list view on mobile
  let activeContent = $state('');
  let activeLoading = $state(false);
  let activeSaving = $state(false);

  let activeReadOnly = $derived.by(() => {
    if (!index || !activeKey) return false;
    const s = index.sections.find((x) => x.key === activeKey);
    return s?.read_only === true;
  });

  let previewAgent = $state<string>('');
  let previewMode = $state<'' | 'root' | 'child'>('');
  let previewParent = $state<string>('');
  let previewTaskSlug = $state<string>('');
  let previewContent = $state('');
  let previewLoading = $state(false);

  // ---------- Agents matrix state ----------
  let agents = $state<AgentMeta[]>([]);
  let canAsk = $state<Record<string, Set<string> | null>>({});
  let savingPolicy = $state<string | null>(null);

  async function loadIndex() {
    try {
      index = await getSystemPromptsIndex();
      if (!previewAgent && index.agents.length > 0) {
        previewAgent = index.agents[0].name;
      }
    } catch (e) {
      logEvent(`system_prompts index: ${e}`, 'err');
    }
  }

  async function loadSection(key: string) {
    activeKey = key;
    activeLoading = true;
    try {
      const r = await getSystemPromptSection(key);
      activeContent = r.content;
    } catch (e) {
      logEvent(`section ${key}: ${e}`, 'err');
      activeContent = '';
    } finally {
      activeLoading = false;
    }
  }

  async function saveSection() {
    if (!activeKey) return;
    activeSaving = true;
    try {
      await setSystemPromptSection(activeKey, activeContent);
      logEvent(`section ${activeKey} saved`, 'ok');
      await loadIndex();
    } catch (e) {
      logEvent(`save ${activeKey}: ${e}`, 'err');
    } finally {
      activeSaving = false;
    }
  }

  async function toggleSection(toggleKey: SystemPromptToggleKey, value: boolean) {
    if (!index) return;
    const prev = index.toggles[toggleKey];
    index = { ...index, toggles: { ...index.toggles, [toggleKey]: value } };
    try {
      const r = await setSystemPromptConfig({ [toggleKey]: value });
      index = { ...index, toggles: r.toggles };
      await loadIndex();
      logEvent(`${toggleKey} = ${value}`, 'ok');
    } catch (e) {
      logEvent(`toggle ${toggleKey}: ${e}`, 'err');
      if (index) index = { ...index, toggles: { ...index.toggles, [toggleKey]: prev } };
    }
  }

  async function loadPreview() {
    previewLoading = true;
    try {
      const r = await getSystemPromptPreview({
        agent: previewAgent || undefined,
        mode: previewMode || undefined,
        parent: previewParent.trim() || undefined,
        task_slug: previewTaskSlug.trim() || undefined
      });
      previewContent = r.content;
    } catch (e) {
      logEvent(`preview: ${e}`, 'err');
      previewContent = '';
    } finally {
      previewLoading = false;
    }
  }

  async function loadAgents() {
    try {
      const [ag, pol] = await Promise.all([listAgentsMeta(), listAgentPolicies()]);
      agents = ag.items;
      const m: Record<string, Set<string> | null> = {};
      for (const a of ag.items) m[a.name] = null;
      for (const p of pol.items) {
        m[p.agent] = p.can_ask === null ? null : new Set(p.can_ask);
      }
      canAsk = m;
    } catch (e) {
      logEvent(`agents matrix: ${e}`, 'err');
    }
  }

  function isChecked(from: string, to: string): boolean {
    const set = canAsk[from];
    if (set === null) return true;
    return set?.has(to) ?? false;
  }

  function isUnrestricted(from: string): boolean {
    return canAsk[from] === null;
  }

  async function toggleCell(from: string, to: string) {
    if (from === to) return;
    const cur = canAsk[from];
    let next: Set<string>;
    if (cur === null) {
      next = new Set(agents.map((a) => a.name).filter((n) => n !== from && n !== to));
    } else {
      next = new Set(cur);
      if (next.has(to)) next.delete(to);
      else next.add(to);
    }
    canAsk = { ...canAsk, [from]: next };
    await persistPolicy(from);
  }

  async function toggleUnrestricted(from: string) {
    if (canAsk[from] === null) {
      canAsk = { ...canAsk, [from]: new Set<string>() };
    } else {
      canAsk = { ...canAsk, [from]: null };
    }
    await persistPolicy(from);
  }

  async function persistPolicy(from: string) {
    savingPolicy = from;
    try {
      const set = canAsk[from];
      const list = set === null ? null : Array.from(set);
      await upsertAgentPolicy(from, list, null);
    } catch (e) {
      logEvent(`policy save: ${e}`, 'err');
      await loadAgents();
    } finally {
      savingPolicy = null;
    }
  }

  function fmtSize(n: number): string {
    if (!n) return '0 B';
    if (n < 1024) return `${n} B`;
    return `${(n / 1024).toFixed(1)} KB`;
  }

  $effect(() => {
    if (tab === 'preview' && previewAgent) {
      loadPreview();
    }
  });

  $effect(() => {
    if (tab === 'agents' && agents.length === 0) {
      loadAgents();
    }
  });

  onMount(loadIndex);

  function backToList() {
    activeKey = null;
  }
</script>

<div class="flex h-full flex-col overflow-hidden">
  <PageHeader title="Settings" />

  <div class="flex-1 overflow-y-auto overflow-x-hidden p-4">
  <!-- Tabs scrollam horizontalmente em mobile (5 tabs nao cabem em 390px).
       Em sm+ cabe inline sem scroll. `whitespace-nowrap` + `shrink-0` em
       cada tab garante que nao quebrem nem comprimam. -->
  <div class="-mx-4 mb-4 flex gap-1 overflow-x-auto border-b border-border px-4">
    <button
      type="button"
      class="inline-flex min-h-tap shrink-0 items-center gap-1.5 whitespace-nowrap border-b-2 px-3 py-2 text-sm font-semibold transition-colors"
      class:border-accent={tab === 'sections'}
      class:text-fg={tab === 'sections'}
      class:border-transparent={tab !== 'sections'}
      class:text-muted={tab !== 'sections'}
      onclick={() => {
        tab = 'sections';
        activeKey = null;
      }}
    >
      <ScrollText class="h-4 w-4" /> Sections
    </button>
    <button
      type="button"
      class="inline-flex min-h-tap shrink-0 items-center gap-1.5 whitespace-nowrap border-b-2 px-3 py-2 text-sm font-semibold transition-colors"
      class:border-accent={tab === 'preview'}
      class:text-fg={tab === 'preview'}
      class:border-transparent={tab !== 'preview'}
      class:text-muted={tab !== 'preview'}
      onclick={() => (tab = 'preview')}
    >
      <Eye class="h-4 w-4" /> Preview
    </button>
    <button
      type="button"
      class="inline-flex min-h-tap shrink-0 items-center gap-1.5 whitespace-nowrap border-b-2 px-3 py-2 text-sm font-semibold transition-colors"
      class:border-accent={tab === 'agents'}
      class:text-fg={tab === 'agents'}
      class:border-transparent={tab !== 'agents'}
      class:text-muted={tab !== 'agents'}
      onclick={() => (tab = 'agents')}
    >
      <Users class="h-4 w-4" /> Agents
    </button>
    <button
      type="button"
      class="inline-flex min-h-tap shrink-0 items-center gap-1.5 whitespace-nowrap border-b-2 px-3 py-2 text-sm font-semibold transition-colors"
      class:border-accent={tab === 'routines'}
      class:text-fg={tab === 'routines'}
      class:border-transparent={tab !== 'routines'}
      class:text-muted={tab !== 'routines'}
      onclick={() => (tab = 'routines')}
    >
      <Clock class="h-4 w-4" /> Routines
    </button>
    <button
      type="button"
      class="inline-flex min-h-tap shrink-0 items-center gap-1.5 whitespace-nowrap border-b-2 px-3 py-2 text-sm font-semibold transition-colors"
      class:border-accent={tab === 'workflows'}
      class:text-fg={tab === 'workflows'}
      class:border-transparent={tab !== 'workflows'}
      class:text-muted={tab !== 'workflows'}
      onclick={() => (tab = 'workflows')}
    >
      <Workflow class="h-4 w-4" /> Workflows
    </button>
    <button
      type="button"
      class="inline-flex min-h-tap shrink-0 items-center gap-1.5 whitespace-nowrap border-b-2 px-3 py-2 text-sm font-semibold transition-colors"
      class:border-accent={tab === 'system'}
      class:text-fg={tab === 'system'}
      class:border-transparent={tab !== 'system'}
      class:text-muted={tab !== 'system'}
      onclick={() => (tab = 'system')}
    >
      <SettingsIcon class="h-4 w-4" /> System
    </button>
  </div>

  {#if tab === 'sections'}
    {#if !index}
      <p class="text-xs text-muted">Loading…</p>
    {:else}
      <p class="mb-3 text-xs text-muted">
        Everything that goes into <code>--append-system-prompt</code> of <code>claude -p</code>.
        Edits take effect on the next invocation — no restart. Toggles disable a section without
        deleting the file. <code>platform.md</code> is a framework invariant (read-only) and ships
        with the agent/web images.
      </p>

      <div class="grid grid-cols-12 gap-3">
        <!-- List (mobile: full when no selection; md: always visible 4-col) -->
        <aside
          class="col-span-12 space-y-3 md:col-span-4"
          class:hidden={activeKey !== null}
          class:md:block={true}
        >
          <div>
            <h3 class="mb-1 text-[10px] font-semibold uppercase tracking-wide text-muted">
              Global sections
            </h3>
            <ul class="divide-y divide-border/40 rounded-md border border-border">
              {#each index.sections as s (s.key)}
                <li class="flex items-center gap-2 px-2 py-2 md:py-1.5">
                  <input
                    type="checkbox"
                    class="h-5 w-5 md:h-4 md:w-4"
                    checked={s.enabled}
                    title="Toggle: {s.toggle_key}"
                    onchange={(e) => toggleSection(s.toggle_key, (e.target as HTMLInputElement).checked)}
                  />
                  <button
                    type="button"
                    class="flex-1 truncate text-left text-sm md:text-xs"
                    class:text-fg={activeKey === s.key}
                    class:font-semibold={activeKey === s.key}
                    class:text-muted={activeKey !== s.key}
                    disabled={s.generated}
                    onclick={() => !s.generated && loadSection(s.key)}
                    title={s.generated ? 'Generated dynamically — no file' : (s.source_path ?? '')}
                  >
                    {s.title}
                  </button>
                  <span class="shrink-0 text-[10px] text-muted">
                    {s.generated ? 'auto' : (s.present ? fmtSize(s.size) : '—')}
                  </span>
                </li>
              {/each}
            </ul>
          </div>

          <div>
            <h3 class="mb-1 text-[10px] font-semibold uppercase tracking-wide text-muted">
              Per-agent CLAUDE.md
            </h3>
            {#if index.agents.length === 0}
              <p class="text-xs text-muted">No active agents.</p>
            {:else}
              <ul class="divide-y divide-border/40 rounded-md border border-border">
                {#each index.agents as a (a.key)}
                  <li class="flex items-center gap-2 px-2 py-2 md:py-1.5">
                    <button
                      type="button"
                      class="flex-1 truncate text-left text-sm md:text-xs"
                      class:text-fg={activeKey === a.key}
                      class:font-semibold={activeKey === a.key}
                      class:text-muted={activeKey !== a.key}
                      onclick={() => loadSection(a.key)}
                      title={a.source_path ?? ''}
                    >
                      <span class="font-mono">{a.name}</span>
                      <span class="ml-1 text-[10px] text-muted">{a.display_name}</span>
                    </button>
                    <span class="shrink-0 text-[10px] text-muted">
                      {a.present ? fmtSize(a.size) : '—'}
                    </span>
                  </li>
                {/each}
              </ul>
              <p class="mt-1 text-[10px] text-muted">
                Global toggle for "Per-agent CLAUDE.md" disables ALL at once (above).
              </p>
            {/if}
          </div>
        </aside>

        <!-- Editor (mobile: full when selection; md: always visible 8-col) -->
        <section
          class="col-span-12 flex min-h-[60vh] flex-col md:col-span-8"
          class:hidden={activeKey === null}
          class:md:flex={true}
        >
          {#if activeKey}
            <div class="mb-2 flex items-center gap-2">
              <button
                type="button"
                onclick={backToList}
                aria-label="Back to sections"
                class="inline-flex min-h-tap min-w-tap items-center justify-center rounded-md text-muted hover:bg-panel2 hover:text-fg md:hidden"
              >
                <ArrowLeft class="h-5 w-5" />
              </button>
              <span class="flex-1 truncate font-mono text-xs text-muted">{activeKey}</span>
              {#if activeReadOnly}
                <span class="rounded-md border border-border px-2 py-1 text-[10px] font-semibold uppercase tracking-wide text-muted">
                  Framework-fixed · read only
                </span>
              {:else}
                <button
                  type="button"
                  onclick={saveSection}
                  disabled={activeSaving || activeLoading}
                  class="inline-flex min-h-tap items-center gap-1 rounded-md bg-accent px-3 py-2 text-xs font-semibold text-on-accent hover:bg-accent/90 disabled:opacity-50"
                >
                  <Save class="h-4 w-4" /> {activeSaving ? 'Saving…' : 'Save'}
                </button>
              {/if}
            </div>
            {#if activeLoading}
              <p class="text-xs text-muted">Loading…</p>
            {:else}
              <textarea
                bind:value={activeContent}
                readonly={activeReadOnly}
                class="w-full flex-1 resize-none rounded-md border border-border bg-bg p-3 font-mono text-xs leading-relaxed focus:border-accent focus:outline-none"
                class:opacity-80={activeReadOnly}
                spellcheck="false"
              ></textarea>
            {/if}
          {:else}
            <div class="hidden h-full items-center justify-center text-xs text-muted md:flex">
              Pick a section on the left to edit.
            </div>
          {/if}
        </section>
      </div>
    {/if}
  {:else if tab === 'preview'}
    {#if !index}
      <p class="text-xs text-muted">Loading…</p>
    {:else}
      <p class="mb-3 text-xs text-muted">
        Shows the exact text sent to <code>--append-system-prompt</code> respecting current toggles.
        Pick an agent to include their <code>CLAUDE.md</code> and the team block filtered by policy.
        Use the simulation fields below to populate the contextual blocks (invocation mode, task state, step instructions) the framework injects in runtime.
      </p>
      <div class="mb-2 flex flex-wrap items-center gap-2">
        <label class="text-xs text-muted" for="prev-agent">Agent:</label>
        <select
          id="prev-agent"
          bind:value={previewAgent}
          class="min-h-tap rounded border border-border bg-bg px-2 py-1.5 text-sm"
        >
          <option value="">— no agent (globals only) —</option>
          {#each index.agents as a (a.key)}
            <option value={a.name}>{a.name} — {a.display_name}</option>
          {/each}
        </select>
        <button
          type="button"
          onclick={loadPreview}
          disabled={previewLoading}
          class="inline-flex min-h-tap items-center gap-1 rounded-md border border-border px-3 py-1.5 text-sm hover:bg-panel2 disabled:opacity-50"
        >
          <RefreshCw class="h-4 w-4" /> {previewLoading ? 'Refreshing…' : 'Refresh'}
        </button>
        <span class="ml-2 text-[10px] text-muted">
          {previewContent.length} chars · ~{Math.round(previewContent.length / 4)} tokens
        </span>
      </div>
      <div class="mb-2 flex flex-wrap items-end gap-2 rounded-md border border-dashed border-border bg-panel2/30 p-2">
        <span class="text-[10px] font-semibold uppercase tracking-wide text-muted">Simulate runtime context</span>
        <label class="text-xs">
          <span class="mb-0.5 block text-muted">Invocation mode</span>
          <select
            bind:value={previewMode}
            class="min-h-tap rounded border border-border bg-bg px-2 py-1.5 text-sm"
          >
            <option value="">— show hint —</option>
            <option value="root">root</option>
            <option value="child">child</option>
          </select>
        </label>
        <label class="text-xs">
          <span class="mb-0.5 block text-muted">Parent agent (when child)</span>
          <input
            type="text"
            bind:value={previewParent}
            placeholder="e.g. product-owner"
            class="min-h-tap rounded border border-border bg-bg px-2 py-1.5 text-sm font-mono"
          />
        </label>
        <label class="text-xs">
          <span class="mb-0.5 block text-muted">Task slug (existing)</span>
          <input
            type="text"
            bind:value={previewTaskSlug}
            placeholder="e.g. fix-login-redirect"
            class="min-h-tap rounded border border-border bg-bg px-2 py-1.5 text-sm font-mono"
          />
        </label>
      </div>
      <pre
        class="h-[60vh] w-full overflow-auto whitespace-pre-wrap rounded-md border border-border bg-bg p-3 font-mono text-xs leading-relaxed"
      >{previewContent || '_(empty — all toggles may be off)_'}</pre>
    {/if}
  {:else if tab === 'agents'}
    <p class="mb-3 text-xs text-muted">
      Matrix of who can ask whom (via <code>ask_agent</code>). Row = asker; column = target.
      Check "any" to allow all targets. Auto-saves on click. Defense-in-depth validation in the
      broker rejects out-of-policy attempts.
    </p>
    {#if agents.length === 0}
      <p class="text-xs text-muted">No agents yet.</p>
    {:else}
      <div class="overflow-x-auto">
        <table class="text-xs">
          <thead class="text-muted">
            <tr>
              <th class="px-2 py-1 text-left">from \ to</th>
              <th class="px-2 py-1 text-center">any</th>
              {#each agents as a (a.name)}
                <th class="px-2 py-1 text-center font-mono text-[10px]">{a.name}</th>
              {/each}
            </tr>
          </thead>
          <tbody>
            {#each agents as from (from.name)}
              <tr class="border-t border-border/40">
                <td class="px-2 py-1 font-semibold">{from.name}</td>
                <td class="px-2 py-1 text-center">
                  <input
                    type="checkbox"
                    class="h-5 w-5 md:h-4 md:w-4"
                    checked={isUnrestricted(from.name)}
                    disabled={savingPolicy === from.name}
                    onchange={() => toggleUnrestricted(from.name)}
                  />
                </td>
                {#each agents as to (to.name)}
                  <td class="px-2 py-1 text-center">
                    {#if from.name === to.name}
                      <span class="text-muted opacity-50">—</span>
                    {:else}
                      <input
                        type="checkbox"
                        class="h-5 w-5 md:h-4 md:w-4"
                        checked={isChecked(from.name, to.name)}
                        disabled={isUnrestricted(from.name) || savingPolicy === from.name}
                        onchange={() => toggleCell(from.name, to.name)}
                      />
                    {/if}
                  </td>
                {/each}
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  {:else if tab === 'routines'}
    <RoutinesPanel />
  {:else if tab === 'workflows'}
    <WorkflowsPanel />
  {:else if tab === 'system'}
    <SystemPanel />
  {/if}
  </div>
</div>
