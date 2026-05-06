<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import uPlot from 'uplot';
  import type { AlignedData, Axis, Options, Series as UPlotSeries } from 'uplot';
  import 'uplot/dist/uPlot.min.css';

  interface Series {
    label: string;
    color: string;
    values: number[];
    yAxis?: number;
  }

  interface Props {
    xs: number[];
    series: Series[];
    height?: number;
  }

  let { xs, series, height = 220 }: Props = $props();

  let containerEl: HTMLDivElement | null = $state(null);
  let chart: uPlot | null = null;

  function cssVar(name: string, fallback: string): string {
    if (typeof document === 'undefined') return fallback;
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }

  function buildOpts(): Options {
    const axisStroke = cssVar('--muted', '#7d8590');
    const gridStroke = cssVar('--border', '#30363d') + '66'; // ~40% alpha pra grid sutil
    const yAxes = Math.max(1, Math.max(...series.map((s) => (s.yAxis ?? 0) + 1)));
    const seriesCfg: UPlotSeries[] = [
      { label: 'time' },
      ...series.map((s) => ({
        label: s.label,
        stroke: s.color,
        width: 1.5,
        scale: s.yAxis === 1 ? 'y2' : 'y'
      }))
    ];
    const axes: Axis[] = [
      { stroke: axisStroke, grid: { stroke: gridStroke }, ticks: { stroke: gridStroke } },
      { stroke: axisStroke, grid: { stroke: gridStroke }, ticks: { stroke: gridStroke }, scale: 'y' }
    ];
    if (yAxes > 1) {
      axes.push({ stroke: axisStroke, side: 1, grid: { show: false }, scale: 'y2' });
    }
    return {
      width: containerEl?.clientWidth || 600,
      height,
      series: seriesCfg,
      axes,
      legend: { show: true, live: true },
      cursor: { y: false },
      scales: { x: { time: true } }
    };
  }

  function buildData(): AlignedData {
    return [xs, ...series.map((s) => s.values)] as unknown as AlignedData;
  }

  function render() {
    if (!containerEl) return;
    chart?.destroy();
    chart = new uPlot(buildOpts(), buildData(), containerEl);
  }

  onMount(render);
  onDestroy(() => chart?.destroy());

  $effect(() => {
    void xs;
    void series;
    if (chart) chart.setData(buildData());
  });
</script>

<div bind:this={containerEl} class="w-full" style="min-height: {height}px"></div>
