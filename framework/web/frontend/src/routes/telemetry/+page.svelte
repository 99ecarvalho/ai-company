<script lang="ts">
  import { onMount } from 'svelte';
  import { Trash2 } from 'lucide-svelte';
  import PageHeader from '$lib/components/ui/PageHeader.svelte';
  import UPlotChart from '$lib/components/UPlotChart.svelte';
  import {
    telemetrySummary,
    telemetryRecent,
    telemetryTimeseries,
    listCostBudgets,
    upsertCostBudget,
    deleteCostBudget,
    type TelemetrySummary,
    type TelemetryRecentRow,
    type TelemetryTimeseriesResponse,
    type CostBudgetRow
  } from '$lib/api';
  import { fmtCost, fmtMs, fmtNum } from '$lib/services/format';
  import { logEvent } from '$lib/stores/ui';

  let win = $state('24h');
  let agentFilter = $state<string>('');
  let modelFilter = $state<string>('');
  let summary: TelemetrySummary | null = $state(null);
  let recent: TelemetryRecentRow[] = $state([]);
  let series = $state<TelemetryTimeseriesResponse | null>(null);
  let budgets = $state<CostBudgetRow[]>([]);
  let loading = $state(false);
  let error = $state<string | null>(null);

  // Add budget form
  let newAgent = $state('');
  let newLimit = $state<number | null>(null);
  let savingBudget = $state(false);

  async function refresh() {
    loading = true;
    error = null;
    const filters = { agent: agentFilter || null, model: modelFilter || null };
    try {
      const [s, r, t, b] = await Promise.all([
        telemetrySummary(win, filters),
        telemetryRecent(20, filters).catch(() => ({ items: [] as TelemetryRecentRow[] })),
        telemetryTimeseries(win, filters).catch(() => null),
        listCostBudgets().catch(() => ({ items: [] as CostBudgetRow[] }))
      ]);
      summary = s;
      recent = r.items;
      series = t;
      budgets = b.items;
    } catch (e) {
      error = String(e);
      logEvent(`telemetry: ${e}`, 'err');
    } finally {
      loading = false;
    }
  }

  // Options for filters: derivadas do unfiltered summary (carregado separadamente)
  // pra nao zerar o dropdown quando um filtro reduz o resultado pra nada.
  let agentOptions = $state<string[]>([]);
  let modelOptions = $state<string[]>([]);

  async function loadFilterOptions() {
    try {
      const s = await telemetrySummary(win);
      agentOptions = s.by_agent.map((a) => a.agent);
      modelOptions = s.by_model.map((m) => m.model);
    } catch {
      // silent — filters just stay empty
    }
  }

  async function addBudget() {
    if (!newAgent.trim() || !newLimit || newLimit <= 0) return;
    savingBudget = true;
    try {
      await upsertCostBudget(newAgent.trim(), newLimit);
      logEvent(`budget set: ${newAgent} = $${newLimit}/day`, 'ok');
      newAgent = '';
      newLimit = null;
      await refresh();
    } catch (e) {
      logEvent(`budget save: ${e}`, 'err');
    } finally {
      savingBudget = false;
    }
  }

  async function removeBudget(agent: string) {
    if (!confirm(`Remove budget for ${agent}?`)) return;
    try {
      await deleteCostBudget(agent);
      await refresh();
    } catch (e) {
      logEvent(`budget delete: ${e}`, 'err');
    }
  }

  const chartXs = $derived(series?.points.map((p) => p.ts) || []);
  const chartSeries = $derived([
    { label: 'runs',     color: '#2f81f7', values: series?.points.map((p) => p.runs) || [] },
    { label: 'cost USD', color: '#e5534b', values: series?.points.map((p) => p.cost_usd) || [], yAxis: 1 },
    { label: 'duration ms', color: '#d29922', values: series?.points.map((p) => p.duration_ms) || [], yAxis: 1 }
  ]);

  onMount(async () => {
    await Promise.all([refresh(), loadFilterOptions()]);
  });
</script>

<div class="flex h-full flex-col overflow-hidden">
  <PageHeader title="Telemetry">
    {#snippet actions()}
    <button
      type="button"
      class="min-h-tap rounded-md border border-border bg-panel2 px-3 py-1.5 text-sm hover:bg-bg"
      onclick={refresh}
    >Refresh</button>
    {/snippet}
  </PageHeader>

  <!-- Toolbar de filtros separada do header. Permite wrap em mobile (3
       selects nao cabem inline com Refresh num PageHeader shrink-0). -->
  <div class="flex flex-wrap items-center gap-2 border-b border-border bg-panel2/50 px-3 py-2 md:px-4">
    <select
      class="min-h-tap rounded-md border border-border bg-panel2 px-2 py-1.5 text-sm"
      bind:value={win}
      onchange={() => { refresh(); loadFilterOptions(); }}
      aria-label="Time window"
    >
      <option value="1h">1h</option>
      <option value="24h">24h</option>
      <option value="7d">7d</option>
      <option value="30d">30d</option>
    </select>
    <select
      class="min-h-tap rounded-md border border-border bg-panel2 px-2 py-1.5 text-sm"
      bind:value={agentFilter}
      onchange={refresh}
      aria-label="Filter by agent"
    >
      <option value="">All agents</option>
      {#each agentOptions as a (a)}
        <option value={a}>{a}</option>
      {/each}
    </select>
    <select
      class="min-h-tap rounded-md border border-border bg-panel2 px-2 py-1.5 text-sm"
      bind:value={modelFilter}
      onchange={refresh}
      aria-label="Filter by model"
    >
      <option value="">All models</option>
      {#each modelOptions as m (m)}
        <option value={m}>{m}</option>
      {/each}
    </select>
  </div>

  <div class="flex-1 overflow-y-auto overflow-x-hidden p-4">
  {#if loading}
    <p class="text-xs text-muted">loading…</p>
  {:else if error}
    <p class="text-xs text-accent2">error: {error}</p>
  {:else if summary}
    {@const totals = summary.totals || {}}
    <div class="mb-4 grid grid-cols-2 gap-2 sm:grid-cols-4">
      <div class="rounded-md border border-border bg-panel2 p-3">
        <div class="text-[10px] uppercase text-muted">runs</div>
        <div class="text-lg font-semibold">{fmtNum(totals.runs ?? 0)}</div>
      </div>
      <div class="rounded-md border border-border bg-panel2 p-3">
        <div class="text-[10px] uppercase text-muted">total cost</div>
        <div class="text-lg font-semibold">{fmtCost(totals.total_cost_usd)}</div>
      </div>
      <div class="rounded-md border border-border bg-panel2 p-3">
        <div class="text-[10px] uppercase text-muted">total time</div>
        <div class="text-lg font-semibold">{fmtMs(totals.total_duration_ms)}</div>
      </div>
      <div class="rounded-md border border-border bg-panel2 p-3">
        <div class="text-[10px] uppercase text-muted">output tokens</div>
        <div class="text-lg font-semibold">{fmtNum(totals.output_tokens ?? 0)}</div>
      </div>
    </div>

    {#if chartXs.length > 1}
      <div class="mb-2 text-xs font-semibold text-muted">Time series ({series?.bucket} bucket)</div>
      <div class="mb-4 rounded-md border border-border bg-panel2 p-2">
        <UPlotChart xs={chartXs} series={chartSeries} height={200} />
      </div>
    {/if}

    {#if summary.by_agent.length}
      <div class="mb-2 text-xs font-semibold text-muted">By agent</div>
      <div class="overflow-x-auto">
        <table class="w-full text-xs">
          <thead class="text-muted">
            <tr class="border-b border-border">
              <th class="px-2 py-1.5 text-left">agent</th>
              <th class="px-2 py-1.5 text-right">runs</th>
              <th class="px-2 py-1.5 text-right">cost</th>
              <th class="px-2 py-1.5 text-right">avg dur.</th>
              <th class="px-2 py-1.5 text-right">in</th>
              <th class="px-2 py-1.5 text-right">out</th>
              <th class="px-2 py-1.5 text-right">cache</th>
              <th class="px-2 py-1.5 text-right">fails</th>
            </tr>
          </thead>
          <tbody>
            {#each summary.by_agent as a (a.agent)}
              <tr class="border-b border-border/40">
                <td class="px-2 py-1 font-semibold">{a.agent}</td>
                <td class="px-2 py-1 text-right">{a.runs}</td>
                <td class="px-2 py-1 text-right">{fmtCost(a.cost_usd)}</td>
                <td class="px-2 py-1 text-right">{fmtMs(a.avg_duration_ms)}</td>
                <td class="px-2 py-1 text-right">{fmtNum(a.input_tokens)}</td>
                <td class="px-2 py-1 text-right">{fmtNum(a.output_tokens)}</td>
                <td class="px-2 py-1 text-right">{fmtNum(a.cache_tokens)}</td>
                <td class="px-2 py-1 text-right">
                  {#if a.failures}
                    <span class="rounded-sm bg-accent2/20 px-1.5 py-0.5 text-accent2">{a.failures}</span>
                  {:else}—{/if}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {:else}
      <p class="text-xs text-muted">No runs in this window yet.</p>
    {/if}

    {#if summary.by_model.length}
      <div class="mt-4 mb-2 text-xs font-semibold text-muted">By model</div>
      <div class="overflow-x-auto">
        <table class="w-full text-xs">
          <thead class="text-muted">
            <tr class="border-b border-border">
              <th class="px-2 py-1.5 text-left">model</th>
              <th class="px-2 py-1.5 text-right">runs</th>
              <th class="px-2 py-1.5 text-right">cost</th>
              <th class="px-2 py-1.5 text-right">avg dur.</th>
              <th class="px-2 py-1.5 text-right">in</th>
              <th class="px-2 py-1.5 text-right">out</th>
              <th class="px-2 py-1.5 text-right">cache</th>
              <th class="px-2 py-1.5 text-right">fails</th>
            </tr>
          </thead>
          <tbody>
            {#each summary.by_model as m (m.model)}
              <tr class="border-b border-border/40">
                <td class="px-2 py-1 font-mono text-[11px]">{m.model}</td>
                <td class="px-2 py-1 text-right">{m.runs}</td>
                <td class="px-2 py-1 text-right">{fmtCost(m.cost_usd)}</td>
                <td class="px-2 py-1 text-right">{fmtMs(m.avg_duration_ms)}</td>
                <td class="px-2 py-1 text-right">{fmtNum(m.input_tokens)}</td>
                <td class="px-2 py-1 text-right">{fmtNum(m.output_tokens)}</td>
                <td class="px-2 py-1 text-right">{fmtNum(m.cache_tokens)}</td>
                <td class="px-2 py-1 text-right">
                  {#if m.failures}
                    <span class="rounded-sm bg-accent2/20 px-1.5 py-0.5 text-accent2">{m.failures}</span>
                  {:else}—{/if}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}

    <!-- Cost budgets section -->
    <div class="mt-4 mb-2 flex items-baseline justify-between">
      <span class="text-xs font-semibold text-muted">Cost budgets (USD/day, UTC)</span>
      <span class="text-[10px] text-muted">scheduler `cost_budget_check` alerts on overrun</span>
    </div>
    {#if budgets.length}
      <div class="mb-3 grid gap-1">
        {#each budgets as b (b.agent)}
          <div class="flex items-center gap-2 rounded-md border border-border bg-panel2 px-2 py-1.5 text-xs">
            <span class="w-32 shrink-0 font-semibold">{b.agent}</span>
            <div class="flex-1">
              <div class="h-1.5 w-full overflow-hidden rounded-sm bg-bg">
                <div
                  class="h-full transition-all"
                  class:bg-accent2={b.over}
                  class:bg-warn={!b.over && b.pct >= 0.8}
                  class:bg-ok={!b.over && b.pct < 0.8}
                  style="width: {Math.min(100, b.pct * 100).toFixed(0)}%"
                ></div>
              </div>
            </div>
            <span class="w-32 shrink-0 text-right font-mono text-[11px]">
              {fmtCost(b.spent_today_usd)} / {fmtCost(b.daily_usd_limit)}
            </span>
            <span
              class="w-12 shrink-0 text-right font-semibold"
              class:text-accent2={b.over}
              class:text-warn={!b.over && b.pct >= 0.8}
            >{(b.pct * 100).toFixed(0)}%</span>
            <button
              type="button"
              class="rounded p-1 text-muted hover:bg-bg hover:text-accent2"
              aria-label="Remove budget"
              title="Remove"
              onclick={() => removeBudget(b.agent)}
            >
              <Trash2 class="h-3 w-3" />
            </button>
          </div>
        {/each}
      </div>
    {/if}
    <form
      onsubmit={(e) => { e.preventDefault(); addBudget(); }}
      class="mb-4 flex flex-col gap-2 xs:flex-row"
    >
      <input
        type="text"
        placeholder="agent name"
        bind:value={newAgent}
        class="min-h-tap flex-1 rounded-md border border-border bg-panel2 px-2 py-2 text-sm"
      />
      <input
        type="number"
        step="0.01"
        min="0.01"
        placeholder="USD/day"
        bind:value={newLimit}
        class="min-h-tap rounded-md border border-border bg-panel2 px-2 py-2 text-sm xs:w-28"
      />
      <button
        type="submit"
        disabled={savingBudget || !newAgent.trim() || !newLimit}
        class="min-h-tap rounded-md bg-accent px-4 py-2 text-sm font-semibold text-on-accent hover:bg-accent/90 disabled:opacity-50"
      >Set</button>
    </form>

    {#if recent.length}
      <div class="mt-4 mb-2 text-xs font-semibold text-muted">Latest runs</div>
      <div class="overflow-x-auto">
        <table class="w-full text-xs">
          <thead class="text-muted">
            <tr class="border-b border-border">
              <th class="px-2 py-1.5 text-left">when</th>
              <th class="px-2 py-1.5 text-left">agent</th>
              <th class="px-2 py-1.5 text-left">model</th>
              <th class="px-2 py-1.5 text-left">topic</th>
              <th class="px-2 py-1.5 text-right">dur</th>
              <th class="px-2 py-1.5 text-right">cost</th>
              <th class="px-2 py-1.5 text-right">turns</th>
            </tr>
          </thead>
          <tbody>
            {#each recent as r (r.id)}
              <tr class="border-b border-border/40">
                <td class="px-2 py-1">
                  {new Date((r.started_at || 0) * 1000).toLocaleTimeString()}
                </td>
                <td class="px-2 py-1 font-semibold">
                  {r.agent}
                  {#if r.exit_code && r.exit_code !== 0}
                    <span class="rounded-sm bg-accent2/20 px-1 text-accent2">fail</span>
                  {/if}
                </td>
                <td class="px-2 py-1 font-mono text-[11px] text-muted">{r.model ?? '—'}</td>
                <td class="px-2 py-1 text-muted">
                  {(r.topic || r.topic_slug || '').slice(0, 40)}
                </td>
                <td class="px-2 py-1 text-right">{fmtMs(r.duration_ms)}</td>
                <td class="px-2 py-1 text-right">{fmtCost(r.total_cost_usd ?? r.cost_usd)}</td>
                <td class="px-2 py-1 text-right">{r.num_turns ?? '—'}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  {/if}
  </div>
</div>
