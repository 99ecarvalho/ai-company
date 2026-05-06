<script lang="ts">
  import { goto } from '$app/navigation';
  import { ArrowRight, Sparkles, Loader2, Check } from 'lucide-svelte';
  import {
    listPhilosophyTemplates,
    onboardProposeAgents,
    onboardApply,
    type PhilosophyTemplate,
    type ProposedAgent,
    ApiError
  } from '$lib/api';
  import { logEvent } from '$lib/stores/ui';

  let step = $state<1 | 2 | 3 | 4>(1);

  // Step 1 — company
  let companyName = $state('');
  let companySector = $state('');
  let companyMission = $state('');
  let companyHumans = $state('');
  let companyFlows = $state('');

  // Step 2 — philosophy
  let templates = $state<PhilosophyTemplate[]>([]);
  let selectedSlugs = $state<Set<string>>(new Set());
  let philosophyExtra = $state('');

  // Step 3 — agents
  let agents = $state<ProposedAgent[]>([]);
  let agentsKept = $state<Set<string>>(new Set());
  let proposing = $state(false);
  let proposeError = $state<string | null>(null);

  // Step 4 — apply
  let applying = $state(false);
  let applyResult = $state<{ created: string[]; errors: { slug: string; error: string }[] } | null>(null);

  $effect(() => {
    if (step === 2 && templates.length === 0) {
      listPhilosophyTemplates()
        .then((r) => (templates = r.items))
        .catch((e) => logEvent(`philosophies: ${e}`, 'err'));
    }
  });

  function buildCompanyMd(): string {
    return [
      '# Company context',
      '',
      '## Company',
      '',
      `- **Name:** ${companyName || '_(fill in)_'}`,
      `- **Sector:** ${companySector || '_(fill in)_'}`,
      `- **Mission:** ${companyMission || '_(fill in)_'}`,
      '',
      '## Human team',
      '',
      companyHumans.trim() || '_(fill in)_',
      '',
      '## Main flows',
      '',
      companyFlows.trim() || '_(fill in)_',
      '',
      '## Conventions',
      '',
      '- Dates in ISO (YYYY-MM-DD).',
      '- Language: en. Code: en.',
      ''
    ].join('\n');
  }

  function buildPhilosophyMd(): string {
    if (selectedSlugs.size === 0) return philosophyExtra.trim() ? `# Philosophy\n\n${philosophyExtra}\n` : '';
    const parts = ['# Operating philosophy', ''];
    if (selectedSlugs.size > 1) {
      parts.push(`_Mix of ${[...selectedSlugs].join(' + ')}._`);
      parts.push('');
    }
    for (const slug of selectedSlugs) {
      const t = templates.find((x) => x.slug === slug);
      if (t) {
        parts.push(`## ${t.title}`);
        parts.push('');
        parts.push(`> ${t.summary}`);
        parts.push('');
      }
    }
    if (philosophyExtra.trim()) {
      parts.push('## Local overrides');
      parts.push('');
      parts.push(philosophyExtra.trim());
      parts.push('');
    }
    return parts.join('\n');
  }

  function toggleTemplate(slug: string) {
    const next = new Set(selectedSlugs);
    if (next.has(slug)) next.delete(slug);
    else next.add(slug);
    selectedSlugs = next;
  }

  async function generateAgents() {
    proposing = true;
    proposeError = null;
    try {
      const r = await onboardProposeAgents(buildCompanyMd(), buildPhilosophyMd());
      agents = r.agents;
      agentsKept = new Set(agents.map((a) => a.slug));
    } catch (e) {
      proposeError = e instanceof ApiError ? e.detail : String(e);
    } finally {
      proposing = false;
    }
  }

  function toggleAgent(slug: string) {
    const next = new Set(agentsKept);
    if (next.has(slug)) next.delete(slug);
    else next.add(slug);
    agentsKept = next;
  }

  async function apply() {
    applying = true;
    try {
      const kept = agents.filter((a) => agentsKept.has(a.slug));
      const r = await onboardApply(buildCompanyMd(), buildPhilosophyMd(), kept);
      applyResult = { created: r.created, errors: r.errors };
      logEvent(`onboard: created ${r.created.length} agents`, 'ok');
    } catch (e) {
      logEvent(`onboard apply: ${e}`, 'err');
    } finally {
      applying = false;
    }
  }

  const canNext1 = $derived(!!companyName.trim());
  const canNext3 = $derived(agentsKept.size > 0);
</script>

<svelte:head><title>Onboarding · Agents</title></svelte:head>

<main class="min-h-dvh bg-bg p-4 md:p-8">
  <div class="mx-auto flex max-w-3xl flex-col gap-6">
    <header class="flex items-center gap-3">
      <Sparkles class="h-6 w-6 text-accent" />
      <h1 class="text-xl font-semibold">Welcome to agent-framework</h1>
    </header>

    <nav class="flex flex-wrap gap-2 text-xs">
      {#each [1, 2, 3, 4] as n}
        <span
          class="rounded-md border px-3 py-1.5 font-mono"
          class:border-accent={step === n}
          class:bg-accent={step === n}
          class:text-on-accent={step === n}
          class:border-border={step !== n}
          class:text-muted={step !== n && step < n}
          class:bg-panel2={step > n}
        >
          {n}. {['Company', 'Philosophy', 'Agents', 'Apply'][n - 1]}
        </span>
      {/each}
    </nav>

    <section class="rounded-md border border-border bg-panel p-4">
      {#if step === 1}
        <h2 class="mb-3 text-base font-semibold">About the company</h2>
        <div class="grid gap-3">
          <label class="grid gap-1 text-xs">
            <span class="text-muted">Name</span>
            <input bind:value={companyName} class="min-h-tap rounded-md border border-border bg-panel2 px-2 py-2 text-sm" />
          </label>
          <label class="grid gap-1 text-xs">
            <span class="text-muted">Sector</span>
            <input bind:value={companySector} placeholder="e.g. SaaS B2B, e-commerce, dev tools" class="min-h-tap rounded-md border border-border bg-panel2 px-2 py-2 text-sm" />
          </label>
          <label class="grid gap-1 text-xs">
            <span class="text-muted">Mission (one sentence)</span>
            <input bind:value={companyMission} class="min-h-tap rounded-md border border-border bg-panel2 px-2 py-2 text-sm" />
          </label>
          <label class="grid gap-1 text-xs">
            <span class="text-muted">Team humans (one per line — optional)</span>
            <textarea bind:value={companyHumans} rows="3" placeholder="e.g. Alice — founder / dev" class="resize-none rounded-md border border-border bg-panel2 px-2 py-2 text-sm font-mono"></textarea>
          </label>
          <label class="grid gap-1 text-xs">
            <span class="text-muted">Main flows (what you do most every day)</span>
            <textarea bind:value={companyFlows} rows="3" placeholder="e.g. 1) discovery; 2) build; 3) ship…" class="resize-none rounded-md border border-border bg-panel2 px-2 py-2 text-sm"></textarea>
          </label>
        </div>
      {:else if step === 2}
        <h2 class="mb-3 text-base font-semibold">Operating philosophy</h2>
        <p class="mb-3 text-xs text-muted">
          Pick one or more philosophy bases. They are skeleton templates
          (placeholders for now). Multiple selections merge into a single
          file. Use the textarea below for local overrides.
        </p>
        <div class="mb-3 grid gap-2">
          {#each templates as t (t.slug)}
            <label class="flex cursor-pointer items-start gap-2 rounded-md border border-border bg-panel2 p-2 text-xs hover:border-accent">
              <input
                type="checkbox"
                checked={selectedSlugs.has(t.slug)}
                onchange={() => toggleTemplate(t.slug)}
                class="mt-0.5 h-5 w-5"
              />
              <div class="flex-1">
                <div class="font-semibold">{t.title}</div>
                <div class="text-muted">{t.summary}</div>
              </div>
            </label>
          {/each}
        </div>
        <label class="grid gap-1 text-xs">
          <span class="text-muted">Local overrides / notes specific to your company</span>
          <textarea bind:value={philosophyExtra} rows="4" class="resize-none rounded-md border border-border bg-panel2 px-2 py-2 text-sm"></textarea>
        </label>
      {:else if step === 3}
        <div class="mb-3 flex flex-wrap items-center justify-between gap-2">
          <h2 class="text-base font-semibold">Proposed agents</h2>
          <button
            type="button"
            onclick={generateAgents}
            disabled={proposing}
            class="inline-flex min-h-tap items-center gap-1.5 rounded-md bg-accent px-3 py-2 text-sm font-semibold text-on-accent hover:bg-accent/90 disabled:opacity-50"
          >
            {#if proposing}
              <Loader2 class="h-4 w-4 animate-spin" /> Generating…
            {:else}
              <Sparkles class="h-4 w-4" /> {agents.length ? 'Re-generate' : 'Generate via Claude'}
            {/if}
          </button>
        </div>
        {#if proposeError}
          <p class="mb-3 text-xs text-accent2">{proposeError}</p>
        {/if}
        {#if !agents.length && !proposing}
          <p class="text-xs text-muted">Click "Generate via Claude" to have the LLM propose agents aligned with your company + philosophy (~30s).</p>
        {/if}
        {#if agents.length}
          <ul class="grid gap-2">
            {#each agents as a (a.slug)}
              <li class="rounded-md border border-border bg-panel2 p-2 text-xs">
                <label class="flex cursor-pointer items-start gap-2">
                  <input
                    type="checkbox"
                    checked={agentsKept.has(a.slug)}
                    onchange={() => toggleAgent(a.slug)}
                    class="mt-1 h-5 w-5"
                  />
                  <div class="flex-1">
                    <div class="flex items-baseline gap-2">
                      <strong>{a.display_name}</strong>
                      <code class="text-[10px] text-muted">{a.slug}</code>
                    </div>
                    <div class="mt-0.5 text-muted">{a.role}</div>
                    <div class="mt-1 text-[10px] italic text-muted">why: {a.why}</div>
                  </div>
                </label>
              </li>
            {/each}
          </ul>
        {/if}
      {:else}
        <h2 class="mb-3 text-base font-semibold">Ready to apply</h2>
        {#if !applyResult}
          <p class="mb-3 text-xs text-muted">
            About to create <strong>{agentsKept.size}</strong> agent(s),
            save CONTEXT.md + philosophy.md, and mark onboarding complete.
            Each agent takes ~10-30s to create (creates user/stream in the
            broker + writes CLAUDE.md + runs reconcile).
          </p>
          <button
            type="button"
            onclick={apply}
            disabled={applying}
            class="inline-flex min-h-tap items-center gap-2 rounded-md bg-accent px-4 py-2 text-sm font-semibold text-on-accent hover:bg-accent/90 disabled:opacity-50"
          >
            {#if applying}
              <Loader2 class="h-5 w-5 animate-spin" /> Applying…
            {:else}
              <Check class="h-5 w-5" /> Apply
            {/if}
          </button>
        {:else}
          <p class="mb-2 text-sm text-ok">
            ✓ {applyResult.created.length} agent(s) created:
            <code>{applyResult.created.join(', ')}</code>
          </p>
          {#if applyResult.errors.length}
            <p class="mb-2 text-xs text-accent2">Partial errors:</p>
            <ul class="mb-3 text-xs text-accent2">
              {#each applyResult.errors as e}
                <li>{e.slug}: {e.error}</li>
              {/each}
            </ul>
          {/if}
          <button
            type="button"
            onclick={() => goto('/', { replaceState: true })}
            class="min-h-tap rounded-md bg-accent px-4 py-2 text-sm font-semibold text-on-accent hover:bg-accent/90"
          >Open PWA</button>
        {/if}
      {/if}
    </section>

    {#if !applyResult}
      <div class="flex justify-between">
        <button
          type="button"
          onclick={() => (step = (Math.max(1, step - 1) as 1 | 2 | 3 | 4))}
          disabled={step === 1}
          class="min-h-tap rounded-md border border-border bg-panel2 px-4 py-2 text-sm hover:bg-bg disabled:opacity-30"
        >Back</button>
        <button
          type="button"
          onclick={() => (step = (Math.min(4, step + 1) as 1 | 2 | 3 | 4))}
          disabled={(step === 1 && !canNext1) || (step === 3 && !canNext3) || step === 4}
          class="inline-flex min-h-tap items-center gap-1.5 rounded-md bg-accent px-4 py-2 text-sm font-semibold text-on-accent hover:bg-accent/90 disabled:opacity-50"
        >Next <ArrowRight class="h-4 w-4" /></button>
      </div>
    {/if}
  </div>
</main>
