<script lang="ts">
  import { onMount } from 'svelte';
  import {
    RefreshCw,
    Save,
    Trash2,
    Plus,
    Pencil,
    ArrowUp,
    ArrowDown,
    X,
    Loader2,
    CircleDot,
    Circle,
    CheckCircle2,
    AlertTriangle
  } from 'lucide-svelte';
  import {
    listWorkflowsExpanded,
    upsertWorkflow,
    deleteWorkflow,
    renameWorkflow,
    listAgentsMeta,
    type WorkflowDef,
    type WorkflowStepOverrides,
    type AgentMeta,
    ApiError
  } from '$lib/api';
  import { logEvent } from '$lib/stores/ui';

  type StepEffort = '' | 'low' | 'medium' | 'high' | 'xhigh' | 'max';

  type DraftStep = {
    name: string;
    agent: string;
    artifact: string;
    next: string[];
    instructions: string;
    /** Runtime overrides applied by claude_runner while the agent is
     *  running this step. Empty strings / undefined = inherit from agent. */
    overrideModel: string;
    overrideEffort: StepEffort;
    /** tri-state: '' = inherit, 'on' = enable, 'off' = disable */
    overrideMemoryEnabled: '' | 'on' | 'off';
    /** kept as a string to preserve empty input without confusing it with 0 */
    overrideMemoryLimit: string;
  };

  const EFFORT_OPTIONS: StepEffort[] = ['', 'low', 'medium', 'high', 'xhigh', 'max'];
  const MODEL_SUGGESTIONS = ['sonnet', 'opus', 'haiku'];

  type Draft = {
    name: string;
    initial_step: string;
    /** D-110: agent that orchestrates the workflow. Empty = falls back to
     *  initial_step.agent. */
    orchestrator: string;
    steps: DraftStep[];
    description: string;
  };

  let workflows = $state<WorkflowDef[]>([]);
  let terminals = $state<string[]>(['done', 'halt', 'human_review']);
  let agents = $state<AgentMeta[]>([]);
  let selectedName = $state<string | null>(null);
  let draft = $state<Draft | null>(null);
  let baseline = $state<string>(''); // JSON of original for dirty detection

  let loading = $state(false);
  let refreshing = $state(false);
  let saving = $state(false);
  let deleting = $state(false);

  onMount(() => {
    void refresh(true);
  });

  async function refresh(first = false) {
    if (first) loading = true;
    else refreshing = true;
    try {
      const [wfResp, agResp] = await Promise.all([listWorkflowsExpanded(), listAgentsMeta()]);
      workflows = wfResp.items;
      terminals = wfResp.terminals;
      agents = agResp.items;
      if (workflows.length > 0 && !selectedName) {
        selectWorkflow(workflows[0].name);
      } else if (selectedName) {
        // reload the currently selected one
        const w = workflows.find((w) => w.name === selectedName);
        if (w) {
          draft = workflowToDraft(w);
          baseline = JSON.stringify(draft);
        } else {
          selectedName = workflows[0]?.name ?? null;
          if (selectedName) selectWorkflow(selectedName);
          else {
            draft = null;
            baseline = '';
          }
        }
      }
    } catch (e) {
      logEvent(`workflows: ${e}`, 'err');
    } finally {
      loading = false;
      refreshing = false;
    }
  }

  function workflowToDraft(wf: WorkflowDef): Draft {
    // Preserve the step order given by steps_ordered first, then append
    // any steps not in the happy path (branches, loops targets).
    const ordered: string[] = [];
    const seen = new Set<string>();
    for (const s of wf.steps_ordered) {
      if (s in wf.steps) {
        ordered.push(s);
        seen.add(s);
      }
    }
    for (const s of Object.keys(wf.steps)) {
      if (!seen.has(s)) ordered.push(s);
    }
    return {
      name: wf.name,
      initial_step: wf.initial_step,
      orchestrator: wf.orchestrator ?? '',
      description: wf.description ?? '',
      steps: ordered.map((name) => {
        const s = wf.steps[name];
        const ov = s.overrides ?? null;
        const memEnabled = ov?.memory?.enabled;
        const memLimit = ov?.memory?.auto_inject_limit;
        return {
          name,
          agent: s.agent ?? '',
          artifact: s.artifact ?? '',
          next: [...s.next],
          instructions: s.instructions ?? '',
          overrideModel: ov?.model ?? '',
          overrideEffort: (ov?.effort ?? '') as StepEffort,
          overrideMemoryEnabled:
            memEnabled === true ? 'on' : memEnabled === false ? 'off' : '',
          overrideMemoryLimit:
            memLimit === undefined || memLimit === null ? '' : String(memLimit)
        };
      })
    };
  }

  function selectWorkflow(name: string) {
    selectedName = name;
    const w = workflows.find((x) => x.name === name);
    if (!w) {
      draft = null;
      baseline = '';
      return;
    }
    draft = workflowToDraft(w);
    baseline = JSON.stringify(draft);
  }

  const dirty = $derived(draft !== null && baseline !== JSON.stringify(draft));

  // ---------- Preview (happy path, mirrors backend _happy_path_order) ----------

  const previewSteps = $derived.by<string[]>(() => {
    if (!draft) return [];
    const stepMap = new Map(draft.steps.map((s) => [s.name, s]));
    const terminalSet = new Set(terminals);
    const ordered: string[] = [];
    const visited = new Set<string>();
    let current: string | undefined = draft.initial_step;
    while (current && !visited.has(current) && !terminalSet.has(current)) {
      const step = stepMap.get(current);
      if (!step) break;
      visited.add(current);
      ordered.push(current);
      let nextStep: string | undefined;
      for (const n of step.next) {
        if (!terminalSet.has(n) && !visited.has(n)) {
          nextStep = n;
          break;
        }
      }
      current = nextStep;
    }
    return ordered;
  });

  const previewTerminal = $derived.by<string | null>(() => {
    if (!draft || previewSteps.length === 0) return null;
    const last = draft.steps.find((s) => s.name === previewSteps[previewSteps.length - 1]);
    if (!last) return null;
    const terminalSet = new Set(terminals);
    // Prefer 'done' over other terminals (happy path).
    if (last.next.includes('done')) return 'done';
    for (const n of last.next) {
      if (terminalSet.has(n)) return n;
    }
    return null;
  });

  // ---------- Client-side validation (gate Save + show inline msg) ----------

  const validationError = $derived.by<string | null>(() => {
    if (!draft) return null;
    if (draft.steps.length === 0) return 'Workflow must have at least one step.';
    const names = draft.steps.map((s) => s.name.trim());
    const seen = new Set<string>();
    for (const n of names) {
      if (!n) return 'Every step needs a name.';
      if (!/^[a-z0-9][a-z0-9_-]{0,63}$/.test(n))
        return `Invalid step name: "${n}" (lowercase/digits/-/_, 1-64 chars).`;
      if (terminals.includes(n)) return `Step name "${n}" collides with a reserved terminal.`;
      if (seen.has(n)) return `Duplicate step name: "${n}".`;
      seen.add(n);
    }
    if (!draft.initial_step || !seen.has(draft.initial_step))
      return `initial_step "${draft.initial_step}" must match one of the declared steps.`;
    const validTargets = new Set<string>([...seen, ...terminals]);
    for (const step of draft.steps) {
      if (step.next.length === 0) return `Step "${step.name}" has empty "next".`;
      for (const t of step.next) {
        if (!validTargets.has(t))
          return `Step "${step.name}" → "${t}" doesn't exist (declare it or pick a terminal).`;
      }
      if (step.overrideMemoryLimit.trim()) {
        const n = Number(step.overrideMemoryLimit);
        if (!Number.isInteger(n) || n < 0)
          return `Step "${step.name}": memory limit must be an integer ≥ 0.`;
      }
    }
    return null;
  });

  function buildOverrides(s: DraftStep): WorkflowStepOverrides | null {
    const ov: WorkflowStepOverrides = {};
    if (s.overrideModel.trim()) ov.model = s.overrideModel.trim();
    if (s.overrideEffort) ov.effort = s.overrideEffort;
    const mem: { enabled?: boolean; auto_inject_limit?: number } = {};
    if (s.overrideMemoryEnabled === 'on') mem.enabled = true;
    else if (s.overrideMemoryEnabled === 'off') mem.enabled = false;
    if (s.overrideMemoryLimit.trim()) {
      const n = Number(s.overrideMemoryLimit);
      if (Number.isInteger(n) && n >= 0) mem.auto_inject_limit = n;
    }
    if (Object.keys(mem).length > 0) ov.memory = mem;
    return Object.keys(ov).length > 0 ? ov : null;
  }

  // ---------- Edit actions ----------

  function updateStep(idx: number, patch: Partial<DraftStep>) {
    if (!draft) return;
    const steps = [...draft.steps];
    steps[idx] = { ...steps[idx], ...patch };
    draft = { ...draft, steps };
  }

  function renameStepInDraft(idx: number, newName: string) {
    if (!draft) return;
    const oldName = draft.steps[idx].name;
    if (oldName === newName) return;
    const steps = draft.steps.map((s, i) => {
      const next = s.next.map((n) => (n === oldName ? newName : n));
      return i === idx ? { ...s, name: newName, next } : { ...s, next };
    });
    const initial_step = draft.initial_step === oldName ? newName : draft.initial_step;
    draft = { ...draft, steps, initial_step };
  }

  function addStep() {
    if (!draft) return;
    const existing = new Set(draft.steps.map((s) => s.name));
    let i = draft.steps.length + 1;
    let name = `step-${i}`;
    while (existing.has(name)) {
      i += 1;
      name = `step-${i}`;
    }
    const newStep: DraftStep = {
      name,
      agent: '',
      artifact: '',
      next: ['done'],
      instructions: '',
      overrideModel: '',
      overrideEffort: '',
      overrideMemoryEnabled: '',
      overrideMemoryLimit: ''
    };
    const steps = [...draft.steps, newStep];
    const initial_step = draft.initial_step || name;
    draft = { ...draft, steps, initial_step };
  }

  function removeStep(idx: number) {
    if (!draft) return;
    const victim = draft.steps[idx].name;
    const steps = draft.steps
      .filter((_, i) => i !== idx)
      .map((s) => ({ ...s, next: s.next.filter((n) => n !== victim) }));
    let initial_step = draft.initial_step;
    if (initial_step === victim) {
      initial_step = steps[0]?.name ?? '';
    }
    draft = { ...draft, steps, initial_step };
  }

  function moveStep(idx: number, dir: -1 | 1) {
    if (!draft) return;
    const target = idx + dir;
    if (target < 0 || target >= draft.steps.length) return;
    const steps = [...draft.steps];
    [steps[idx], steps[target]] = [steps[target], steps[idx]];
    draft = { ...draft, steps };
  }

  function addNextTarget(idx: number, target: string) {
    if (!draft || !target) return;
    const step = draft.steps[idx];
    if (step.next.includes(target)) return;
    updateStep(idx, { next: [...step.next, target] });
  }

  function removeNextTarget(idx: number, target: string) {
    if (!draft) return;
    const step = draft.steps[idx];
    updateStep(idx, { next: step.next.filter((n) => n !== target) });
  }

  function discard() {
    if (!selectedName) return;
    if (dirty && !confirm('Discard local changes?')) return;
    selectWorkflow(selectedName);
  }

  // ---------- Server actions ----------

  async function save() {
    if (!draft || !selectedName || validationError) return;
    saving = true;
    try {
      const stepsRecord: Record<
        string,
        {
          agent: string | null;
          artifact: string | null;
          next: string[];
          instructions?: string | null;
          overrides?: WorkflowStepOverrides | null;
        }
      > = {};
      for (const s of draft.steps) {
        stepsRecord[s.name] = {
          agent: s.agent.trim() || null,
          artifact: s.artifact.trim() || null,
          next: [...s.next],
          instructions: s.instructions.trim() ? s.instructions : null,
          overrides: buildOverrides(s)
        };
      }
      await upsertWorkflow(selectedName, {
        initial_step: draft.initial_step,
        orchestrator: draft.orchestrator.trim() || null,
        steps: stepsRecord,
        description: draft.description.trim() || null
      });
      logEvent(`workflow ${selectedName} saved`, 'ok');
      await refresh();
    } catch (e) {
      const msg = e instanceof ApiError ? e.detail : String(e);
      logEvent(`save workflow: ${msg}`, 'err');
    } finally {
      saving = false;
    }
  }

  async function createWorkflow() {
    const name = prompt('New workflow name (lowercase / digits / - / _):', '');
    if (!name) return;
    const trimmed = name.trim();
    if (!/^[a-z0-9][a-z0-9_-]{0,63}$/.test(trimmed)) {
      logEvent('Invalid workflow name.', 'err');
      return;
    }
    if (workflows.some((w) => w.name === trimmed)) {
      logEvent(`Workflow "${trimmed}" already exists.`, 'err');
      return;
    }
    saving = true;
    try {
      await upsertWorkflow(trimmed, {
        initial_step: 'start',
        steps: {
          start: { agent: null, artifact: null, next: ['done'] }
        },
        description: null
      });
      logEvent(`workflow ${trimmed} created`, 'ok');
      selectedName = trimmed;
      await refresh();
    } catch (e) {
      const msg = e instanceof ApiError ? e.detail : String(e);
      logEvent(`create workflow: ${msg}`, 'err');
    } finally {
      saving = false;
    }
  }

  async function doRename() {
    if (!selectedName) return;
    const next = prompt(`Rename workflow "${selectedName}" to:`, selectedName);
    if (!next) return;
    const trimmed = next.trim();
    if (trimmed === selectedName) return;
    saving = true;
    try {
      await renameWorkflow(selectedName, trimmed);
      logEvent(`workflow renamed to ${trimmed}`, 'ok');
      selectedName = trimmed;
      await refresh();
    } catch (e) {
      const msg = e instanceof ApiError ? e.detail : String(e);
      logEvent(`rename workflow: ${msg}`, 'err');
    } finally {
      saving = false;
    }
  }

  async function doDelete() {
    if (!selectedName) return;
    if (!confirm(`Delete workflow "${selectedName}"? Tasks that reference it will fall back to permissive mode.`))
      return;
    deleting = true;
    try {
      await deleteWorkflow(selectedName);
      logEvent(`workflow ${selectedName} deleted`, 'ok');
      const deleted = selectedName;
      selectedName = workflows.find((w) => w.name !== deleted)?.name ?? null;
      await refresh();
    } catch (e) {
      const msg = e instanceof ApiError ? e.detail : String(e);
      logEvent(`delete workflow: ${msg}`, 'err');
    } finally {
      deleting = false;
    }
  }

  // ---------- Helpers for UI ----------

  function stepIsTerminal(name: string): boolean {
    return terminals.includes(name);
  }

  function stepNamesExcluding(exclude: string): string[] {
    if (!draft) return [];
    return draft.steps.map((s) => s.name).filter((n) => n !== exclude);
  }
</script>

<div class="flex flex-col gap-3">
  <div class="flex flex-wrap items-center justify-between gap-2">
    <p class="max-w-2xl text-xs text-muted">
      Workflow taxonomy for this instance. Framework only knows the terminals
      (<code>done</code>, <code>halt</code>, <code>human_review</code>); every
      other step name is yours to define. Saved changes take effect on the
      next <code>complete_phase</code> — no restart needed.
    </p>
    <div class="flex items-center gap-2">
      <button
        type="button"
        class="inline-flex min-h-tap items-center gap-1 rounded-md border border-border bg-panel2 px-3 py-1.5 text-xs hover:bg-bg disabled:opacity-50"
        onclick={() => refresh()}
        disabled={refreshing}
      >
        <RefreshCw class="h-3.5 w-3.5 {refreshing ? 'animate-spin' : ''}" />
        Refresh
      </button>
      <button
        type="button"
        class="inline-flex min-h-tap items-center gap-1 rounded-md bg-accent px-3 py-1.5 text-xs font-semibold text-on-accent hover:opacity-90 disabled:opacity-50"
        onclick={createWorkflow}
        disabled={saving}
      >
        <Plus class="h-3.5 w-3.5" /> New workflow
      </button>
    </div>
  </div>

  {#if loading}
    <div class="px-3 py-8 text-center text-xs text-muted">Loading…</div>
  {:else if workflows.length === 0}
    <div class="px-3 py-8 text-center text-xs text-muted">
      No workflows yet. Click <b>New workflow</b> to create the first one.
    </div>
  {:else}
    <!-- Workflow picker -->
    <div class="flex flex-wrap items-center gap-2">
      <label class="text-xs text-muted" for="wf-select">Workflow:</label>
      <select
        id="wf-select"
        bind:value={selectedName}
        onchange={(e) => selectWorkflow((e.target as HTMLSelectElement).value)}
        class="min-h-tap rounded border border-border bg-bg px-2 py-1.5 text-sm"
      >
        {#each workflows as w (w.name)}
          <option value={w.name}>{w.name}</option>
        {/each}
      </select>
      <button
        type="button"
        class="inline-flex min-h-tap items-center gap-1 rounded-md border border-border bg-panel2 px-2.5 py-1.5 text-xs hover:bg-bg disabled:opacity-50"
        onclick={doRename}
        disabled={!selectedName || saving}
        title="Rename current workflow"
      >
        <Pencil class="h-3.5 w-3.5" /> Rename
      </button>
      <button
        type="button"
        class="inline-flex min-h-tap items-center gap-1 rounded-md border border-warn/50 bg-panel2 px-2.5 py-1.5 text-xs text-warn hover:bg-warn/10 disabled:opacity-50"
        onclick={doDelete}
        disabled={!selectedName || deleting}
      >
        {#if deleting}
          <Loader2 class="h-3.5 w-3.5 animate-spin" />
        {:else}
          <Trash2 class="h-3.5 w-3.5" />
        {/if}
        Delete
      </button>
      {#if dirty}
        <span class="rounded bg-warn/20 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-warn">
          unsaved
        </span>
      {/if}
    </div>

    {#if draft}
      <!-- Preview happy-path -->
      <div class="rounded-md border border-border bg-panel2/40 p-3">
        <div class="mb-1 text-[10px] font-semibold uppercase tracking-wide text-muted">
          Happy-path preview
        </div>
        <div class="flex flex-wrap items-center gap-1 text-xs">
          {#if previewSteps.length === 0}
            <span class="text-muted">(pick an initial step that exists)</span>
          {:else}
            {#each previewSteps as s, i (s + i)}
              <span class="inline-flex items-center gap-1 rounded bg-panel px-2 py-0.5 font-mono">
                {#if s === draft.initial_step}
                  <CircleDot class="h-3 w-3 text-accent" />
                {:else}
                  <Circle class="h-3 w-3 text-muted" />
                {/if}
                {s}
              </span>
              <span class="text-muted">→</span>
            {/each}
            {#if previewTerminal}
              <span
                class="inline-flex items-center gap-1 rounded bg-panel px-2 py-0.5 font-mono"
                class:text-ok={previewTerminal === 'done'}
                class:text-warn={previewTerminal === 'halt' || previewTerminal === 'human_review'}
              >
                <CheckCircle2 class="h-3 w-3" />
                {previewTerminal}
              </span>
            {:else}
              <span class="text-muted italic">(no terminal from last step — branch)</span>
            {/if}
          {/if}
        </div>
      </div>

      <!-- Top-level fields -->
      <div class="grid grid-cols-1 gap-3 md:grid-cols-3">
        <label class="text-xs">
          <span class="mb-1 block text-muted">Initial step</span>
          <select
            bind:value={draft.initial_step}
            class="min-h-tap w-full rounded border border-border bg-bg px-2 py-1.5 text-sm font-mono"
          >
            {#each draft.steps as s (s.name)}
              <option value={s.name}>{s.name}</option>
            {/each}
          </select>
        </label>
        <label class="text-xs">
          <span class="mb-1 block text-muted">
            Orchestrator
            <span class="text-muted/60" title="Agent that owns the supervisor conv for tasks in this workflow. Empty = falls back to initial_step.agent.">
              ⓘ
            </span>
          </span>
          <select
            bind:value={draft.orchestrator}
            class="min-h-tap w-full rounded border border-border bg-bg px-2 py-1.5 text-sm font-mono"
          >
            <option value="">— fallback to initial_step.agent —</option>
            {#each agents as a (a.name)}
              <option value={a.name}>{a.name}</option>
            {/each}
          </select>
        </label>
        <label class="text-xs">
          <span class="mb-1 block text-muted">Description (optional)</span>
          <input
            type="text"
            bind:value={draft.description}
            placeholder="What is this workflow used for?"
            class="min-h-tap w-full rounded border border-border bg-bg px-2 py-1.5 text-sm"
          />
        </label>
      </div>

      <!-- Steps editor -->
      <div class="flex flex-col gap-2">
        {#each draft.steps as step, idx (idx)}
          <div class="rounded-md border border-border bg-panel2/40 p-3">
            <div class="mb-2 flex flex-wrap items-center gap-2">
              <span class="text-[10px] font-semibold uppercase tracking-wide text-muted">
                step {idx + 1}
              </span>
              {#if step.name === draft.initial_step}
                <span class="rounded bg-accent/20 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-accent">
                  initial
                </span>
              {/if}
              <div class="ml-auto flex gap-1">
                <button
                  type="button"
                  aria-label="Move up"
                  class="rounded border border-border bg-panel2 p-1 text-muted hover:bg-bg hover:text-fg disabled:opacity-40"
                  onclick={() => moveStep(idx, -1)}
                  disabled={idx === 0}
                >
                  <ArrowUp class="h-3.5 w-3.5" />
                </button>
                <button
                  type="button"
                  aria-label="Move down"
                  class="rounded border border-border bg-panel2 p-1 text-muted hover:bg-bg hover:text-fg disabled:opacity-40"
                  onclick={() => moveStep(idx, 1)}
                  disabled={idx === draft.steps.length - 1}
                >
                  <ArrowDown class="h-3.5 w-3.5" />
                </button>
                <button
                  type="button"
                  aria-label="Remove step"
                  class="rounded border border-warn/40 bg-panel2 p-1 text-warn hover:bg-warn/10 disabled:opacity-40"
                  onclick={() => removeStep(idx)}
                  disabled={draft.steps.length === 1}
                  title={draft.steps.length === 1 ? 'A workflow needs at least one step' : 'Remove this step'}
                >
                  <Trash2 class="h-3.5 w-3.5" />
                </button>
              </div>
            </div>

            <div class="grid grid-cols-1 gap-2 md:grid-cols-3">
              <label class="text-xs">
                <span class="mb-1 block text-muted">Name</span>
                <input
                  type="text"
                  value={step.name}
                  oninput={(e) => renameStepInDraft(idx, (e.currentTarget as HTMLInputElement).value)}
                  class="min-h-tap w-full rounded border border-border bg-bg px-2 py-1.5 text-sm font-mono"
                />
              </label>
              <label class="text-xs">
                <span class="mb-1 block text-muted">Default agent</span>
                <select
                  value={step.agent}
                  onchange={(e) => updateStep(idx, { agent: (e.currentTarget as HTMLSelectElement).value })}
                  class="min-h-tap w-full rounded border border-border bg-bg px-2 py-1.5 text-sm"
                >
                  <option value="">— none (requires explicit next_agent) —</option>
                  {#each agents as a (a.name)}
                    <option value={a.name}>{a.name}</option>
                  {/each}
                  {#if step.agent && !agents.some((a) => a.name === step.agent)}
                    <option value={step.agent}>{step.agent} (unknown)</option>
                  {/if}
                </select>
              </label>
              <label class="text-xs">
                <span class="mb-1 block text-muted">Artifact (optional)</span>
                <input
                  type="text"
                  value={step.artifact}
                  oninput={(e) => updateStep(idx, { artifact: (e.currentTarget as HTMLInputElement).value })}
                  placeholder="e.g. 01-plano.md"
                  class="min-h-tap w-full rounded border border-border bg-bg px-2 py-1.5 text-sm font-mono"
                />
              </label>
            </div>

            <div class="mt-2 text-xs">
              <span class="mb-1 block text-muted">Next (valid targets)</span>
              <div class="flex flex-wrap items-center gap-1">
                {#each step.next as target (target)}
                  {@const isTerm = stepIsTerminal(target)}
                  <span
                    class="inline-flex items-center gap-1 rounded px-2 py-0.5 font-mono text-xs"
                    class:bg-ok={isTerm && target === 'done'}
                    class:bg-warn={isTerm && (target === 'halt' || target === 'human_review')}
                    class:text-on-accent={isTerm}
                    class:bg-panel={!isTerm}
                    class:border={!isTerm}
                    class:border-border={!isTerm}
                  >
                    {target}
                    <button
                      type="button"
                      aria-label="Remove {target}"
                      class="opacity-70 hover:opacity-100"
                      onclick={() => removeNextTarget(idx, target)}
                    >
                      <X class="h-3 w-3" />
                    </button>
                  </span>
                {/each}
                <select
                  class="min-h-tap rounded border border-border bg-panel2 px-2 py-1 text-xs"
                  value=""
                  onchange={(e) => {
                    const v = (e.currentTarget as HTMLSelectElement).value;
                    if (v) addNextTarget(idx, v);
                    (e.currentTarget as HTMLSelectElement).value = '';
                  }}
                >
                  <option value="">+ add…</option>
                  <optgroup label="Steps">
                    {#each stepNamesExcluding(step.name) as n (n)}
                      {#if !step.next.includes(n)}
                        <option value={n}>{n}</option>
                      {/if}
                    {/each}
                  </optgroup>
                  <optgroup label="Terminals">
                    {#each terminals as t (t)}
                      {#if !step.next.includes(t)}
                        <option value={t}>{t}</option>
                      {/if}
                    {/each}
                  </optgroup>
                </select>
              </div>
            </div>

            <!-- Step instructions (markdown injected into the agent's system prompt) -->
            <details class="mt-3 group" open={!!step.instructions}>
              <summary class="cursor-pointer select-none text-xs text-muted hover:text-fg">
                <span class="inline-flex items-center gap-2">
                  <span class="font-semibold uppercase tracking-wide text-[10px]">Instructions</span>
                  {#if step.instructions.trim()}
                    <span class="rounded bg-accent/15 px-1.5 py-0.5 text-[10px] text-accent">
                      {step.instructions.trim().split('\n').length} lines
                    </span>
                  {:else}
                    <span class="rounded bg-panel px-1.5 py-0.5 text-[10px] text-muted">empty</span>
                  {/if}
                  <span class="text-muted">— markdown injected into the system prompt when the agent enters this step.</span>
                </span>
              </summary>
              <textarea
                value={step.instructions}
                oninput={(e) => updateStep(idx, { instructions: (e.currentTarget as HTMLTextAreaElement).value })}
                rows="10"
                placeholder="Optional markdown. E.g. 'You are the assigned executor. Do not edit code in this phase. Sequence: 1. memory_recall... 2. ...'"
                class="mt-2 w-full rounded border border-border bg-bg px-2 py-1.5 font-mono text-xs leading-relaxed"
              ></textarea>
            </details>

            <!-- Runtime overrides: model/effort/memory per step. Empty = inherit from agent. -->
            <details
              class="mt-3 group"
              open={
                !!step.overrideModel.trim() ||
                !!step.overrideEffort ||
                step.overrideMemoryEnabled !== '' ||
                !!step.overrideMemoryLimit.trim()
              }
            >
              <summary class="cursor-pointer select-none text-xs text-muted hover:text-fg">
                <span class="inline-flex items-center gap-2">
                  <span class="font-semibold uppercase tracking-wide text-[10px]">Runtime overrides</span>
                  {#if !!step.overrideModel.trim() || !!step.overrideEffort || step.overrideMemoryEnabled !== '' || !!step.overrideMemoryLimit.trim()}
                    <span class="rounded bg-accent/15 px-1.5 py-0.5 text-[10px] text-accent">active</span>
                  {:else}
                    <span class="rounded bg-panel px-1.5 py-0.5 text-[10px] text-muted">inherits agent</span>
                  {/if}
                  <span class="text-muted">— override the agent's `model`/`effort`/`memory` in this phase. Uses the agent's creds.</span>
                </span>
              </summary>
              <div class="mt-2 grid gap-2 sm:grid-cols-2">
                <label class="flex flex-col gap-1 text-xs">
                  <span class="text-muted">Model</span>
                  <input
                    type="text"
                    list="step-model-suggestions-{idx}"
                    value={step.overrideModel}
                    oninput={(e) => updateStep(idx, { overrideModel: (e.currentTarget as HTMLInputElement).value })}
                    placeholder="(inherit)"
                    class="rounded border border-border bg-bg px-2 py-1.5 font-mono text-xs"
                  />
                  <datalist id="step-model-suggestions-{idx}">
                    {#each MODEL_SUGGESTIONS as m}
                      <option value={m}></option>
                    {/each}
                  </datalist>
                </label>
                <label class="flex flex-col gap-1 text-xs">
                  <span class="text-muted">Effort</span>
                  <select
                    value={step.overrideEffort}
                    onchange={(e) => updateStep(idx, { overrideEffort: (e.currentTarget as HTMLSelectElement).value as StepEffort })}
                    class="rounded border border-border bg-bg px-2 py-1.5 font-mono text-xs"
                  >
                    {#each EFFORT_OPTIONS as eo}
                      <option value={eo}>{eo === '' ? '(inherit)' : eo}</option>
                    {/each}
                  </select>
                </label>
                <label class="flex flex-col gap-1 text-xs">
                  <span class="text-muted">Memory enabled</span>
                  <select
                    value={step.overrideMemoryEnabled}
                    onchange={(e) => updateStep(idx, { overrideMemoryEnabled: (e.currentTarget as HTMLSelectElement).value as DraftStep['overrideMemoryEnabled'] })}
                    class="rounded border border-border bg-bg px-2 py-1.5 font-mono text-xs"
                  >
                    <option value="">(inherit)</option>
                    <option value="on">true</option>
                    <option value="off">false</option>
                  </select>
                </label>
                <label class="flex flex-col gap-1 text-xs">
                  <span class="text-muted">Memory auto_inject_limit</span>
                  <input
                    type="number"
                    min="0"
                    step="1"
                    value={step.overrideMemoryLimit}
                    oninput={(e) => updateStep(idx, { overrideMemoryLimit: (e.currentTarget as HTMLInputElement).value })}
                    placeholder="(inherit)"
                    class="rounded border border-border bg-bg px-2 py-1.5 font-mono text-xs"
                  />
                </label>
              </div>
            </details>
          </div>
        {/each}
        <button
          type="button"
          class="inline-flex items-center justify-center gap-1 self-start rounded-md border border-dashed border-border bg-panel2 px-3 py-2 text-xs text-muted hover:bg-bg hover:text-fg"
          onclick={addStep}
        >
          <Plus class="h-3.5 w-3.5" /> Add step
        </button>
      </div>

      <!-- Save bar -->
      <div class="sticky bottom-0 flex flex-wrap items-center justify-between gap-2 rounded-md border border-border bg-panel p-2">
        <div class="flex min-w-0 items-center gap-2 text-xs">
          {#if validationError}
            <AlertTriangle class="h-4 w-4 shrink-0 text-warn" />
            <span class="truncate text-warn">{validationError}</span>
          {:else if dirty}
            <span class="text-muted">Unsaved changes.</span>
          {:else}
            <span class="text-muted">Up to date.</span>
          {/if}
        </div>
        <div class="flex gap-2">
          <button
            type="button"
            class="inline-flex min-h-tap items-center gap-1 rounded-md border border-border bg-panel2 px-3 py-1.5 text-xs hover:bg-bg disabled:opacity-50"
            onclick={discard}
            disabled={!dirty || saving}
          >
            Discard
          </button>
          <button
            type="button"
            class="inline-flex min-h-tap items-center gap-1 rounded-md bg-accent px-3 py-1.5 text-xs font-semibold text-on-accent hover:opacity-90 disabled:opacity-50"
            onclick={save}
            disabled={!dirty || saving || validationError !== null}
          >
            {#if saving}
              <Loader2 class="h-3.5 w-3.5 animate-spin" />
            {:else}
              <Save class="h-3.5 w-3.5" />
            {/if}
            Save
          </button>
        </div>
      </div>
    {/if}
  {/if}
</div>

