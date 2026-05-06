<script lang="ts">
  import { onMount } from 'svelte';
  import { RefreshCw, RotateCcw, Save, Loader2 } from 'lucide-svelte';
  import {
    listRoutines,
    updateRoutine,
    resetRoutine,
    type NativeRoutine
  } from '$lib/stores/scheduler';
  import { logEvent } from '$lib/stores/ui';

  let routines = $state<NativeRoutine[]>([]);
  let loading = $state(false);
  let refreshing = $state(false);

  // Edits locais por id antes do save. key=id, value={cron_override, enabled}
  let drafts = $state<Record<string, { cron_override: string; enabled: boolean; dirty: boolean }>>({});
  let saving = $state<Record<string, boolean>>({});

  onMount(() => {
    void refresh();
  });

  async function refresh() {
    if (routines.length === 0) loading = true;
    else refreshing = true;
    try {
      const r = await listRoutines();
      routines = r.items;
      const next: typeof drafts = {};
      for (const rt of routines) {
        next[rt.id] = {
          cron_override: rt.cron_override ?? '',
          enabled: rt.enabled,
          dirty: false
        };
      }
      drafts = next;
    } catch (e) {
      logEvent(`list routines: ${e}`, 'err');
    } finally {
      loading = false;
      refreshing = false;
    }
  }

  function onCronChange(id: string, val: string) {
    const base = drafts[id];
    if (!base) return;
    drafts = { ...drafts, [id]: { ...base, cron_override: val, dirty: true } };
  }

  function onEnabledChange(id: string, val: boolean) {
    const base = drafts[id];
    if (!base) return;
    drafts = { ...drafts, [id]: { ...base, enabled: val, dirty: true } };
  }

  async function onSave(id: string) {
    const d = drafts[id];
    if (!d) return;
    saving = { ...saving, [id]: true };
    try {
      await updateRoutine(id, {
        cron_override: d.cron_override.trim() || null,
        enabled: d.enabled
      });
      logEvent(`routine ${id} saved`, 'ok');
      await refresh();
    } catch (e) {
      logEvent(`save routine: ${e}`, 'err');
    } finally {
      saving = { ...saving, [id]: false };
    }
  }

  async function onReset(id: string) {
    saving = { ...saving, [id]: true };
    try {
      await resetRoutine(id);
      logEvent(`routine ${id} reset to default`, 'ok');
      await refresh();
    } catch (e) {
      logEvent(`reset routine: ${e}`, 'err');
    } finally {
      saving = { ...saving, [id]: false };
    }
  }

  function actionBadgeClass(action: string): string {
    if (action.startsWith('backup_')) return 'bg-ok/20 text-ok';
    if (action.startsWith('cleanup_')) return 'bg-panel2 text-muted';
    if (action === 'cost_budget_check') return 'bg-warn/20 text-warn';
    return 'bg-panel2 text-fg';
  }
</script>

<div class="flex flex-col gap-3">
  <div class="flex items-center justify-between">
    <p class="max-w-2xl text-xs text-muted">
      Platform routines shipped with the framework (backups, cleanups, cost checks).
      Toggle individual jobs on/off and override their cron without restarting the scheduler.
      For custom scheduled jobs (reminders, standups, post_message recurrences) see
      <a href="/scheduler" class="text-accent underline">Scheduler</a>.
    </p>
    <button
      type="button"
      class="inline-flex shrink-0 items-center gap-1 rounded-md border border-border bg-panel2 px-3 py-1.5 text-xs hover:bg-bg disabled:opacity-50"
      onclick={refresh}
      disabled={refreshing}
    >
      <RefreshCw class="h-3.5 w-3.5 {refreshing ? 'animate-spin' : ''}" />
      Refresh
    </button>
  </div>

  {#if loading}
    <div class="px-3 py-8 text-center text-xs text-muted">Loading…</div>
  {:else if routines.length === 0}
    <div class="px-3 py-8 text-center text-xs text-muted">No native routines found.</div>
  {:else}
    {#each routines as r (r.id)}
      {@const d = drafts[r.id] || { cron_override: '', enabled: true, dirty: false }}
      <div class="rounded-md border border-border bg-panel2/40 p-3">
        <div class="mb-1 flex flex-wrap items-center gap-2">
          <span class="font-mono text-sm font-semibold text-fg">{r.id}</span>
          <span class="rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide {actionBadgeClass(r.action)}">
            {r.action}
          </span>
          {#if !d.enabled}
            <span class="rounded bg-panel2 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-muted">disabled</span>
          {/if}
          {#if r.cron_override}
            <span class="rounded bg-accent/20 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-accent">overridden</span>
          {/if}
        </div>

        {#if r.description}
          <p class="mb-2 max-w-3xl text-xs text-muted">{r.description}</p>
        {/if}

        <div class="flex flex-col gap-2 xs:flex-row xs:items-end">
          <label class="flex-1 text-xs">
            <span class="mb-1 block text-muted">
              Cron override (blank = use framework default)
            </span>
            <input
              type="text"
              value={d.cron_override}
              oninput={(e) => onCronChange(r.id, e.currentTarget.value)}
              placeholder={r.default_cron}
              class="min-h-tap w-full rounded border border-border bg-panel px-2 py-1.5 text-sm font-mono"
            />
            <span class="mt-0.5 block text-[10px] text-muted">
              default: <code class="font-mono">{r.default_cron}</code>
              · effective: <code class="font-mono text-fg">{r.effective_cron}</code>
            </span>
          </label>

          <label class="flex min-h-tap items-center gap-2 text-xs">
            <input
              type="checkbox"
              checked={d.enabled}
              onchange={(e) => onEnabledChange(r.id, e.currentTarget.checked)}
              class="h-4 w-4"
            />
            <span>Enabled</span>
          </label>

          <div class="flex gap-2">
            <button
              type="button"
              class="inline-flex min-h-tap items-center gap-1 rounded-md border border-border bg-panel2 px-3 py-1.5 text-xs hover:bg-bg disabled:opacity-50"
              onclick={() => onReset(r.id)}
              disabled={saving[r.id] || (!r.cron_override && r.enabled)}
              title="Remove override — back to framework default"
            >
              <RotateCcw class="h-3.5 w-3.5" />
              Reset
            </button>
            <button
              type="button"
              class="inline-flex min-h-tap items-center gap-1 rounded-md bg-accent px-3 py-1.5 text-xs font-semibold text-on-accent hover:opacity-90 disabled:opacity-50"
              onclick={() => onSave(r.id)}
              disabled={saving[r.id] || !d.dirty}
            >
              {#if saving[r.id]}
                <Loader2 class="h-3.5 w-3.5 animate-spin" />
              {:else}
                <Save class="h-3.5 w-3.5" />
              {/if}
              Save
            </button>
          </div>
        </div>

        {#if Object.keys(r.params).length > 0}
          <div class="mt-2 flex flex-wrap gap-x-3 gap-y-0.5 text-[10px] text-muted">
            {#each Object.entries(r.params) as [k, v] (k)}
              <span><span class="font-mono">{k}</span>: <span class="font-mono text-fg">{JSON.stringify(v)}</span></span>
            {/each}
          </div>
        {/if}
      </div>
    {/each}
  {/if}
</div>
