<script lang="ts">
  import Overlay from './Overlay.svelte';
  import {
    hireDraft,
    hireApply,
    type HireDraftRequest,
    type HireDraftResponse
  } from '$lib/api';
  import { refreshStreams } from '$lib/stores/streams';
  import { logEvent } from '$lib/stores/ui';

  let step = $state<1 | 2>(1);
  let drafting = $state(false);
  let applying = $state(false);
  let statusMsg = $state<string>('');
  let applyMsg = $state<string>('');

  // Step 1 form
  let name = $state('');
  let display_name = $state('');
  let description = $state('');
  let responsibilities = $state('');
  let non_responsibilities = $state('');
  let style = $state('');
  let workflow_participant = $state(false);
  let needs_bash = $state(true);
  let needs_web = $state(false);
  let writeCompany = $state(true);
  let writeOrch = $state(false);
  let writeRepos = $state(false);
  let readCompany = $state(true);
  let readOrch = $state(false);
  let readRepos = $state(false);

  // Step 2 (editable draft)
  let yamlEntry = $state('');
  let claudeMd = $state('');

  function collect(): HireDraftRequest {
    const wa: string[] = [];
    if (writeCompany) wa.push('company');
    if (writeOrch) wa.push('orchestrator');
    if (writeRepos) wa.push('repos');
    const ra: string[] = [];
    if (readCompany) ra.push('company');
    if (readOrch) ra.push('orchestrator');
    if (readRepos) ra.push('repos');
    return {
      name: name.trim(),
      display_name: display_name.trim(),
      description: description.trim(),
      responsibilities: responsibilities.trim(),
      non_responsibilities: non_responsibilities.trim(),
      style: style.trim(),
      workflow_participant,
      needs_bash,
      needs_web,
      write_access: wa,
      read_access: ra
    };
  }

  async function onDraft() {
    const data = collect();
    if (!data.name || !data.display_name || !data.description) {
      statusMsg = 'name, display_name and description are required';
      return;
    }
    drafting = true;
    statusMsg = 'Claude is drafting the profile (~30s)…';
    try {
      const draft: HireDraftResponse = await hireDraft(data);
      yamlEntry = draft.yaml_entry;
      claudeMd = draft.claude_md;
      statusMsg = '';
      step = 2;
    } catch (e) {
      statusMsg = `error: ${e}`;
    } finally {
      drafting = false;
    }
  }

  async function onApply() {
    applying = true;
    applyMsg = 'Applying (~1min — bot, stream, dirs, override)…';
    try {
      const result = await hireApply({
        name: name.trim(),
        yaml_entry: yamlEntry,
        claude_md: claudeMd
      });
      applyMsg = `✓ hired: ${result.note || result.name}`;
      logEvent(`new agent hired: ${result.name}`, 'ok');
      setTimeout(() => refreshStreams(), 2000);
    } catch (e) {
      applyMsg = `error: ${e}`;
    } finally {
      applying = false;
    }
  }

  function back() {
    step = 1;
    applyMsg = '';
  }
</script>

<Overlay id="hire" title="Hire agent" width="max-w-3xl">
  {#if step === 1}
    <div class="grid gap-3">
      <label class="grid gap-1 text-xs">
        <span class="text-muted">Slug (no spaces, lowercase)</span>
        <input
          class="min-h-tap rounded-md border border-border bg-panel2 px-2 py-2 text-sm"
          bind:value={name}
          placeholder="finance"
        />
      </label>
      <label class="grid gap-1 text-xs">
        <span class="text-muted">Display name</span>
        <input
          class="min-h-tap rounded-md border border-border bg-panel2 px-2 py-2 text-sm"
          bind:value={display_name}
          placeholder="Finance"
        />
      </label>
      <label class="grid gap-1 text-xs">
        <span class="text-muted">One-line description</span>
        <input
          class="min-h-tap rounded-md border border-border bg-panel2 px-2 py-2 text-sm"
          bind:value={description}
          placeholder="Owns the daily cash-flow"
        />
      </label>
      <label class="grid gap-1 text-xs">
        <span class="text-muted">Responsibilities</span>
        <textarea
          rows="3"
          class="min-h-tap rounded-md border border-border bg-panel2 px-2 py-2 text-sm"
          bind:value={responsibilities}
        ></textarea>
      </label>
      <label class="grid gap-1 text-xs">
        <span class="text-muted">NOT responsible for</span>
        <textarea
          rows="2"
          class="min-h-tap rounded-md border border-border bg-panel2 px-2 py-2 text-sm"
          bind:value={non_responsibilities}
        ></textarea>
      </label>
      <label class="grid gap-1 text-xs">
        <span class="text-muted">Style / tone</span>
        <input
          class="min-h-tap rounded-md border border-border bg-panel2 px-2 py-2 text-sm"
          bind:value={style}
          placeholder="Direct, no fluff"
        />
      </label>

      <div class="grid grid-cols-1 gap-3 text-sm xs:grid-cols-3">
        <label class="inline-flex items-center gap-1.5">
          <input type="checkbox" bind:checked={workflow_participant} /> workflow participant
        </label>
        <label class="inline-flex items-center gap-1.5">
          <input type="checkbox" bind:checked={needs_bash} /> needs bash
        </label>
        <label class="inline-flex items-center gap-1.5">
          <input type="checkbox" bind:checked={needs_web} /> needs web
        </label>
      </div>

      <div class="grid grid-cols-1 gap-3 text-sm xs:grid-cols-2">
        <fieldset class="rounded-md border border-border p-2">
          <legend class="px-1 text-[10px] uppercase text-muted">Write access</legend>
          <label class="block"><input type="checkbox" bind:checked={writeCompany} /> company</label>
          <label class="block"><input type="checkbox" bind:checked={writeOrch} /> orchestrator</label>
          <label class="block"><input type="checkbox" bind:checked={writeRepos} /> repos</label>
        </fieldset>
        <fieldset class="rounded-md border border-border p-2">
          <legend class="px-1 text-[10px] uppercase text-muted">Read access</legend>
          <label class="block"><input type="checkbox" bind:checked={readCompany} /> company</label>
          <label class="block"><input type="checkbox" bind:checked={readOrch} /> orchestrator</label>
          <label class="block"><input type="checkbox" bind:checked={readRepos} /> repos</label>
        </fieldset>
      </div>

      <div class="flex flex-wrap items-center gap-2">
        <button
          type="button"
          class="min-h-tap rounded-md bg-accent px-4 py-2 text-sm font-semibold text-on-accent hover:bg-accent/90 disabled:opacity-50"
          onclick={onDraft}
          disabled={drafting}
        >Generate draft</button>
        {#if statusMsg}<span class="text-xs text-muted">{statusMsg}</span>{/if}
      </div>
    </div>
  {:else}
    <div class="grid gap-3">
      <label class="grid gap-1 text-xs">
        <span class="text-muted">YAML entry (instance/agents/agents.yaml)</span>
        <textarea
          rows="10"
          class="rounded-md border border-border bg-panel2 px-2 py-1.5 font-mono text-[11px]"
          bind:value={yamlEntry}
        ></textarea>
      </label>
      <label class="grid gap-1 text-xs">
        <span class="text-muted">CLAUDE.md (agent prompt)</span>
        <textarea
          rows="14"
          class="rounded-md border border-border bg-panel2 px-2 py-1.5 font-mono text-[11px]"
          bind:value={claudeMd}
        ></textarea>
      </label>
      <div class="flex flex-wrap items-center gap-2">
        <button
          type="button"
          class="min-h-tap rounded-md border border-border bg-panel2 px-4 py-2 text-sm hover:bg-bg"
          onclick={back}
          disabled={applying}
        >Back</button>
        <button
          type="button"
          class="min-h-tap rounded-md bg-accent px-4 py-2 text-sm font-semibold text-on-accent hover:bg-accent/90 disabled:opacity-50"
          onclick={onApply}
          disabled={applying}
        >Apply (hire)</button>
        {#if applyMsg}<span class="text-xs text-muted">{applyMsg}</span>{/if}
      </div>
    </div>
  {/if}
</Overlay>
