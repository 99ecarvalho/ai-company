<script lang="ts">
  import {
    Wrench, AlertTriangle, ChevronRight, Copy, Check, FileSearch,
    Loader2, FileText, Maximize2, Minimize2
  } from 'lucide-svelte';
  import { fileDownloadUrl, liveEventFull, type LiveEvent, type WorkflowDef } from '$lib/api';
  import { fmtClock, fmtBytes } from '$lib/services/format';
  import { normalizeFilePath } from '$lib/services/markdown';
  import { isViewable } from '$lib/services/paths';
  import { fileViewerTarget, openOverlay } from '$lib/stores/ui';

  interface Props {
    event: LiveEvent;
    /** Paired tool_result event for this tool_use, if any. Parent panel
     *  computes pairing by temporal proximity (no tool_use_id exists). */
    result?: LiveEvent | null;
    /** Workflow of the current task, used to resolve next_agent default for
     *  `__complete_phase` friendly label when not explicit in the input. */
    workflowDef?: WorkflowDef | null;
  }
  let { event, result = null, workflowDef = null }: Props = $props();

  const isResult = $derived(event.kind === 'tool_result');
  const isError = $derived(isResult && event.data?.is_error === true);
  // Tool_result sem erro e ruido quando renderizado solto — ja esta pareado
  // com o tool_use e mostrado no painel OUT. Omite.
  const hide = $derived(isResult && !isError);

  // ---- Friendly name dispatch ----
  //
  // All built-in Claude Code tools + MCP framework tools get a short label
  // (e.g. `Read package.json`, `Ask architect: why ...`) instead of raw JSON.
  // Parsing is defensive: any failure falls back to event.summary.

  function clamp(s: string, n: number): string {
    const t = s.trim();
    return t.length > n ? t.slice(0, n - 1) + '…' : t;
  }

  function basename(p: string): string {
    const slash = p.lastIndexOf('/');
    return slash >= 0 ? p.slice(slash + 1) : p;
  }

  function hostOf(url: string): string {
    try {
      return new URL(url).host || url;
    } catch {
      return url;
    }
  }

  function stripNs(tool: string): string {
    // MCP tools come as `mcp__ai_company__ask_agent` or `__ask_agent`.
    // Strip the namespace prefix(es) but keep the bare name.
    if (tool.includes('__')) {
      const parts = tool.split('__').filter(Boolean);
      return parts[parts.length - 1] || tool;
    }
    return tool;
  }

  function formatTool(tool: string, input: Record<string, unknown>): string | null {
    const bare = stripNs(tool);

    // Built-in Claude Code tools
    switch (bare) {
      case 'Read': {
        const fp = typeof input.file_path === 'string' ? input.file_path : '';
        if (!fp) return null;
        const off = typeof input.offset === 'number' ? input.offset : null;
        const lim = typeof input.limit === 'number' ? input.limit : null;
        if (off !== null && lim !== null) {
          return `Read ${basename(fp)}:L${off}-${off + lim}`;
        }
        return `Read ${basename(fp)}`;
      }
      case 'Write': {
        const fp = typeof input.file_path === 'string' ? input.file_path : '';
        return fp ? `Write ${basename(fp)}` : null;
      }
      case 'Edit':
      case 'MultiEdit':
      case 'NotebookEdit': {
        const fp = typeof input.file_path === 'string' ? input.file_path : '';
        return fp ? `${bare} ${basename(fp)}` : null;
      }
      case 'Grep': {
        const pat = typeof input.pattern === 'string' ? input.pattern : '';
        if (!pat) return null;
        const path = typeof input.path === 'string' ? input.path : '';
        return path ? `Grep ${clamp(pat, 40)} in ${basename(path)}` : `Grep ${clamp(pat, 60)}`;
      }
      case 'Glob': {
        const pat = typeof input.pattern === 'string' ? input.pattern : '';
        return pat ? `Glob ${clamp(pat, 60)}` : null;
      }
      case 'Bash': {
        const desc = typeof input.description === 'string' ? input.description.trim() : '';
        const cmd = typeof input.command === 'string' ? input.command : '';
        if (desc) return `Bash ${clamp(desc, 60)}`;
        if (cmd) return `Bash ${clamp(cmd, 60)}`;
        return null;
      }
      case 'WebFetch': {
        const url = typeof input.url === 'string' ? input.url : '';
        return url ? `Fetch ${hostOf(url)}` : null;
      }
      case 'WebSearch': {
        const q = typeof input.query === 'string' ? input.query : '';
        return q ? `Search ${clamp(q, 60)}` : null;
      }
      case 'Task':
      case 'Agent': {
        const sub = typeof input.subagent_type === 'string' ? input.subagent_type : '';
        const desc = typeof input.description === 'string' ? input.description : '';
        if (sub && desc) return `Agent ${sub}: ${clamp(desc, 50)}`;
        if (sub) return `Agent ${sub}`;
        if (desc) return `Agent ${clamp(desc, 60)}`;
        return null;
      }
      case 'TodoWrite': {
        const todos = Array.isArray(input.todos) ? input.todos : null;
        return todos ? `Todos updated (${todos.length})` : 'Todos updated';
      }

      // Framework MCP tools
      case 'ask_agent': {
        const target = typeof input.target_agent === 'string' ? input.target_agent : '';
        const q = typeof input.question === 'string' ? input.question : '';
        if (target && q) return `Ask ${target}: ${clamp(q, 60)}`;
        if (target) return `Ask ${target}`;
        return null;
      }
      case 'ask_agents_many': {
        const asks = Array.isArray(input.asks) ? input.asks : null;
        return asks ? `Ask ${asks.length} agents in parallel` : null;
      }
      case 'ask_human': {
        const q = typeof input.question === 'string' ? input.question : '';
        return q ? `Ask human: ${clamp(q, 60)}` : 'Ask human';
      }
      case 'notify_human': {
        const m = typeof input.message === 'string' ? input.message : '';
        return m ? `Notify: ${clamp(m, 60)}` : 'Notify human';
      }
      case 'complete_phase': {
        const next = typeof input.next === 'string' ? input.next : '';
        const nextAgent = typeof input.next_agent === 'string' ? input.next_agent : '';
        const wfDefault =
          next && workflowDef?.steps?.[next]?.agent ? workflowDef.steps[next].agent : '';
        const isTerminal = next === 'done' || next === 'halt' || next === 'human_review';
        if (!next) return 'Complete phase';
        if (isTerminal) return `Complete phase → ${next}`;
        const resolved = nextAgent || wfDefault;
        return resolved ? `Handoff → ${resolved}` : `Complete phase → ${next}`;
      }
      case 'create_worktree': {
        const slug = typeof input.task_slug === 'string' ? input.task_slug : '';
        const repo = typeof input.repo === 'string' ? input.repo : '';
        if (slug && repo) return `Worktree ${repo} for ${slug}`;
        if (slug) return `Worktree for ${slug}`;
        return null;
      }
      case 'cleanup_worktrees': {
        const slug = typeof input.task_slug === 'string' ? input.task_slug : '';
        return slug ? `Cleanup worktrees (${slug})` : 'Cleanup worktrees';
      }
      case 'get_task_state': {
        const slug = typeof input.task_slug === 'string' ? input.task_slug : '';
        return slug ? `Task state (${slug})` : 'Task state';
      }
      case 'memory_save':
      case 'memory_edit': {
        const key = typeof input.key === 'string' ? input.key : '';
        return key ? `Memory ${bare === 'memory_save' ? 'save' : 'edit'}: ${clamp(key, 40)}` : null;
      }
      case 'memory_recall': {
        const q = typeof input.query === 'string' ? input.query : '';
        return q ? `Memory recall: ${clamp(q, 40)}` : 'Memory recall';
      }
      case 'memory_list': {
        const tag = typeof input.tag === 'string' ? input.tag : '';
        return tag ? `Memory list: ${clamp(tag, 40)}` : 'Memory list';
      }
      case 'memory_delete': {
        const key = typeof input.key === 'string' ? input.key : '';
        return key ? `Memory delete: ${clamp(key, 40)}` : null;
      }
      case 'backlog_add': {
        const slug = typeof input.slug === 'string' ? input.slug : '';
        const title = typeof input.title === 'string' ? input.title : '';
        return slug ? `Backlog add: ${slug}` : title ? `Backlog add: ${clamp(title, 50)}` : null;
      }
      case 'backlog_list': {
        const status = typeof input.status === 'string' ? input.status : '';
        return status ? `Backlog list: ${status}` : 'Backlog list';
      }
      case 'backlog_update': {
        const slug = typeof input.slug === 'string' ? input.slug : '';
        return slug ? `Backlog update: ${slug}` : null;
      }
      case 'backlog_promote': {
        const slug = typeof input.slug === 'string' ? input.slug : '';
        return slug ? `Backlog promote: ${slug}` : null;
      }
      case 'task_list': {
        return 'List tasks';
      }
      case 'reopen_task': {
        const slug = typeof input.task_slug === 'string' ? input.task_slug : '';
        return slug ? `Reopen ${slug}` : null;
      }
      default:
        return null;
    }
  }

  interface Formatted {
    label: string;
    hasInput: boolean;
  }

  function parseToolDisplay(ev: LiveEvent): Formatted {
    if (ev.kind !== 'tool_use') {
      return { label: ev.summary || '—', hasInput: false };
    }
    const tool = typeof ev.data?.tool === 'string' ? (ev.data.tool as string) : '';
    const raw = typeof ev.data?.input === 'string' ? (ev.data.input as string) : '';
    let input: Record<string, unknown> = {};
    if (raw) {
      try {
        input = JSON.parse(raw);
      } catch {
        input = {};
      }
    } else if (ev.data?.input && typeof ev.data.input === 'object') {
      input = ev.data.input as Record<string, unknown>;
    }
    const friendly = tool ? formatTool(tool, input) : null;
    return {
      label: friendly ?? ev.summary ?? '—',
      hasInput: Boolean(raw) || Object.keys(input).length > 0,
    };
  }

  const display = $derived(parseToolDisplay(event));

  // Pretty-printed input for expanded IN panel.
  function prettyInput(ev: LiveEvent): string {
    const raw = ev.data?.input;
    if (typeof raw === 'string') {
      try {
        return JSON.stringify(JSON.parse(raw), null, 2);
      } catch {
        return raw;
      }
    }
    if (raw && typeof raw === 'object') {
      try {
        return JSON.stringify(raw, null, 2);
      } catch {
        return '';
      }
    }
    return '';
  }

  // Extract a readable output from a paired tool_result. Tries common shapes:
  // content (string/array blocks), output, stdout, text, then summary, then
  // the whole data object as JSON.
  const OUT_CAP = 2000;
  function prettyOutput(r: LiveEvent): string {
    const d = r.data || {};
    const cand = (d as Record<string, unknown>).content;
    if (typeof cand === 'string' && cand.trim()) return cand;
    if (Array.isArray(cand)) {
      const parts: string[] = [];
      for (const block of cand) {
        if (block && typeof block === 'object') {
          const b = block as Record<string, unknown>;
          const t = b.text;
          if (typeof t === 'string') parts.push(t);
          else if (typeof b.type === 'string' && typeof b.content === 'string')
            parts.push(b.content);
        } else if (typeof block === 'string') {
          parts.push(block);
        }
      }
      const joined = parts.join('\n').trim();
      if (joined) return joined;
    }
    const stdout = (d as Record<string, unknown>).stdout;
    if (typeof stdout === 'string' && stdout.trim()) return stdout;
    const output = (d as Record<string, unknown>).output;
    if (typeof output === 'string' && output.trim()) return output;
    const text = (d as Record<string, unknown>).text;
    if (typeof text === 'string' && text.trim()) return text;
    if (r.summary && r.summary.trim() && r.summary !== 'ok') return r.summary;
    try {
      return JSON.stringify(d, null, 2);
    } catch {
      return '';
    }
  }

  /** If `s` parses as valid JSON, return pretty-printed version. Otherwise
   *  the original string unchanged. Used to upgrade OUT blocks that happen
   *  to contain raw JSON (tool outputs, API responses, etc). */
  function prettifyJsonIfAny(s: string): string {
    const trimmed = s.trim();
    if (!trimmed) return s;
    const first = trimmed[0];
    if (first !== '{' && first !== '[') return s;
    try {
      const parsed = JSON.parse(trimmed);
      if (parsed && typeof parsed === 'object') {
        return JSON.stringify(parsed, null, 2);
      }
    } catch {
      /* not JSON — keep as is */
    }
    return s;
  }

  const inputBlock = $derived(event.kind === 'tool_use' ? prettyInput(event) : '');
  const outputRawRaw = $derived(result ? prettyOutput(result) : '');
  const outputRaw = $derived(prettifyJsonIfAny(outputRawRaw));
  const outputTruncated = $derived(outputRaw.length > OUT_CAP);
  const outputBlock = $derived(
    outputTruncated ? outputRaw.slice(0, OUT_CAP) + `\n… [${outputRaw.length - OUT_CAP} more chars]` : outputRaw
  );

  // Copy feedback: shows ✓ for 1.2s after click.
  let copiedIn = $state(false);
  let copiedOut = $state(false);
  let copyInTimer: ReturnType<typeof setTimeout> | null = null;
  let copyOutTimer: ReturnType<typeof setTimeout> | null = null;

  async function copyText(
    text: string,
    setFlag: (v: boolean) => void,
    clearTimer: () => void,
    setTimer: (t: ReturnType<typeof setTimeout>) => void
  ): Promise<void> {
    if (!text) return;
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        // Fallback for non-secure contexts — requires a temporary textarea.
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
      }
      setFlag(true);
      clearTimer();
      setTimer(setTimeout(() => setFlag(false), 1200));
    } catch {
      /* ignore — permission denied or no clipboard */
    }
  }

  function copyIn(e: MouseEvent) {
    e.stopPropagation();
    void copyText(
      inputBlock,
      (v) => (copiedIn = v),
      () => { if (copyInTimer) clearTimeout(copyInTimer); },
      (t) => (copyInTimer = t)
    );
  }

  function copyOut(e: MouseEvent) {
    e.stopPropagation();
    // Copia o raw completo (nao o truncado). Se "view full" ja foi
    // carregado, copia o full do banco (~200KB cap); senao copia o
    // outputRaw (capado em 5KB pelo SSE).
    void copyText(
      fullOutput ?? outputRaw,
      (v) => (copiedOut = v),
      () => { if (copyOutTimer) clearTimeout(copyOutTimer); },
      (t) => (copyOutTimer = t)
    );
  }

  const canExpand = $derived(
    Boolean(inputBlock) || Boolean(outputBlock) || display.label !== (event.summary ?? '')
  );
  let expanded = $state(false);
  function toggle() {
    if (canExpand) expanded = !expanded;
  }

  const resultIsError = $derived(result?.data?.is_error === true);

  // Backend (claude_runner) capa output em ~5KB no payload SSE; full
  // (ate ~200KB) fica no JSONB do banco e eh fetched on-demand.
  const outputBackendTruncated = $derived(
    result?.data?.output_truncated === true
  );
  const outputFullLen = $derived(
    typeof result?.data?.output_full_len === 'number'
      ? (result.data.output_full_len as number)
      : null
  );
  let fullOutput = $state<string | null>(null);
  let loadingFull = $state(false);
  let fullError = $state<string | null>(null);

  async function loadFullOutput() {
    if (!result || loadingFull || fullOutput) return;
    loadingFull = true;
    fullError = null;
    try {
      const r = await liveEventFull(result.id);
      const d = r.data || {};
      const f = (d as Record<string, unknown>).output_full;
      if (typeof f === 'string') {
        fullOutput = prettifyJsonIfAny(f);
      } else {
        // Fallback: backend antigo que nao tem output_full ainda — re-extrai.
        fullOutput = prettifyJsonIfAny(prettyOutput(r as unknown as LiveEvent));
      }
    } catch (e) {
      fullError = String(e);
    } finally {
      loadingFull = false;
    }
  }

  // Texto efetivamente exibido no OUT: full quando carregado, senao inline cap.
  const outputDisplay = $derived(fullOutput ?? outputBlock);

  // Tools que operam num arquivo unico ganham botao "open" no header pra
  // abrir o arquivo no FileViewer sem sair da conversa (regra: nunca
  // redirect). O OUT panel mostra o conteudo cru que o Claude recebeu;
  // o "open" mostra o estado atual do FS (pode divergir se houve edit
  // posterior). Os dois coexistem.
  function extractFileOpenPath(ev: LiveEvent): string | null {
    if (ev.kind !== 'tool_use') return null;
    const tool = typeof ev.data?.tool === 'string' ? (ev.data.tool as string) : '';
    if (!['Read', 'Write', 'Edit', 'MultiEdit', 'NotebookEdit'].includes(stripNs(tool))) return null;
    const raw = ev.data?.input;
    let inp: Record<string, unknown> = {};
    if (typeof raw === 'string') {
      try { inp = JSON.parse(raw); } catch { return null; }
    } else if (raw && typeof raw === 'object') {
      inp = raw as Record<string, unknown>;
    }
    const fp = typeof inp.file_path === 'string' ? inp.file_path : '';
    return fp || null;
  }
  const readFilePath = $derived(extractFileOpenPath(event));

  function openReadFile(e: MouseEvent) {
    e.stopPropagation();
    if (!readFilePath) return;
    // Claude Code usa paths absolutos (`/workspace/company/...`); o file
    // viewer backend so aceita relativos (`company/`, `repos/`, `agents/`).
    // `normalizeFilePath` strip o prefixo /workspace/ ou /app/.
    const path = normalizeFilePath(readFilePath);
    // Regra: nunca redirect que tire o humano da conversa.
    // Diretorio → nova aba pra /files (sem sair da aba atual).
    // File viewable → FileViewer overlay inline.
    // File nao-viewable → download via anchor (fica na aba atual).
    if (path.endsWith('/') || path === '') {
      const cleaned = path.replace(/\/+$/, '');
      window.open(`/files?path=${encodeURIComponent(cleaned)}`, '_blank', 'noopener');
      return;
    }
    if (isViewable(path)) {
      fileViewerTarget.set({ path });
      openOverlay('fileViewer');
    } else {
      const link = document.createElement('a');
      link.href = fileDownloadUrl(path);
      link.download = basename(path);
      link.rel = 'noopener';
      link.click();
    }
  }

  // Expand/collapse dos painéis IN/OUT — max-h-[400px] default; click no
  // botao toggle remove o cap pra ver tudo sem scrollar internamente.
  let inputExpanded = $state(false);
  let outputExpanded = $state(false);
  function toggleInputExpanded(e: MouseEvent) {
    e.stopPropagation();
    inputExpanded = !inputExpanded;
  }
  function toggleOutputExpanded(e: MouseEvent) {
    e.stopPropagation();
    outputExpanded = !outputExpanded;
  }
</script>

{#if hide}{:else if isError}
  <div class="flex items-start gap-2 rounded-md border border-accent2/40 bg-accent2/10 px-2 py-1 font-mono text-[11px] text-accent2">
    <AlertTriangle class="mt-0.5 h-3 w-3 shrink-0" />
    <span class="w-[76px] shrink-0 text-[9px] uppercase opacity-80">tool_error</span>
    <span class="flex-1 whitespace-pre-wrap break-words">{event.summary || 'error'}</span>
    <span class="shrink-0 text-[9px] opacity-60">{fmtClock(event.ts)}</span>
  </div>
{:else}
  <div class="flex flex-col font-mono text-[11px] {resultIsError ? 'rounded-md border border-accent2/40 bg-accent2/5 text-accent2' : 'text-muted'}">
    <div class="flex w-full items-start gap-2 px-2 py-0.5 {canExpand ? (resultIsError ? 'hover:bg-accent2/10' : 'hover:bg-panel2/40') : ''}">
      <button
        type="button"
        class="flex flex-1 items-start gap-2 text-left min-w-0 {canExpand ? 'cursor-pointer' : 'cursor-default'}"
        onclick={toggle}
        disabled={!canExpand}
      >
        {#if canExpand}
          <ChevronRight class="mt-0.5 h-3 w-3 shrink-0 {resultIsError ? 'text-accent2' : 'text-warn'} transition-transform {expanded ? 'rotate-90' : ''}" />
        {:else}
          <Wrench class="mt-0.5 h-3 w-3 shrink-0 {resultIsError ? 'text-accent2' : 'text-warn'}" />
        {/if}
        <span class="w-[76px] shrink-0 text-[9px] uppercase opacity-70">tool_use</span>
        <span class="flex-1 truncate {resultIsError ? 'opacity-100 font-medium' : 'opacity-90'}">{display.label}</span>
      </button>
      {#if resultIsError}
        <span class="inline-flex shrink-0 items-center gap-1 rounded bg-accent2/15 px-1.5 py-0.5 text-[9px] font-semibold uppercase text-accent2" title="Tool returned is_error=true">
          <AlertTriangle class="h-3 w-3" />
          error
        </span>
      {/if}
      {#if readFilePath}
        <!-- Tools com file_path (Read/Write/Edit/MultiEdit/NotebookEdit):
             link no header abre o arquivo no overlay FileViewer sem sair
             da conversa. stopPropagation pra o click nao toggle o expand. -->
        <button
          type="button"
          onclick={openReadFile}
          class="inline-flex shrink-0 items-center gap-1 rounded border border-border bg-panel2/50 px-1.5 py-0.5 text-[9px] text-accent hover:bg-accent/10 hover:underline"
          title="Open {readFilePath} in file viewer"
        >
          <FileText class="h-3 w-3" />
          open
        </button>
      {/if}
      <span class="shrink-0 text-[9px] opacity-50 pt-0.5">{fmtClock(event.ts)}</span>
    </div>

    {#if expanded}
      {#if inputBlock}
        <div class="mx-2 mb-1 overflow-hidden rounded border border-border bg-bg">
          <div class="flex items-center justify-between border-b border-border bg-panel2/50 px-2 py-0.5 text-[9px] uppercase tracking-wider text-muted">
            <span>IN</span>
            <span class="flex items-center gap-1">
              <button
                type="button"
                onclick={toggleInputExpanded}
                class="inline-flex items-center gap-1 rounded px-1 py-0.5 text-[9px] hover:bg-panel2 hover:text-fg"
                title={inputExpanded ? 'Collapse' : 'Expand (remove height limit)'}
                aria-label={inputExpanded ? 'Collapse input' : 'Expand input'}
              >
                {#if inputExpanded}
                  <Minimize2 class="h-3 w-3" />
                  <span>collapse</span>
                {:else}
                  <Maximize2 class="h-3 w-3" />
                  <span>expand</span>
                {/if}
              </button>
              <button
                type="button"
                onclick={copyIn}
                class="inline-flex items-center gap-1 rounded px-1 py-0.5 text-[9px] hover:bg-panel2 hover:text-fg"
                title={copiedIn ? 'Copied' : 'Copy'}
                aria-label={copiedIn ? 'Copied' : 'Copy input'}
              >
                {#if copiedIn}
                  <Check class="h-3 w-3 text-ok" />
                  <span class="text-ok">copied</span>
                {:else}
                  <Copy class="h-3 w-3" />
                  <span>copy</span>
                {/if}
              </button>
            </span>
          </div>
          <pre class="m-0 {inputExpanded ? '' : 'max-h-[100px]'} overflow-auto p-2 text-[11px] text-fg/90 whitespace-pre-wrap break-words">{inputBlock}</pre>
        </div>
      {/if}
      {#if outputBlock}
        <div
          class="mx-2 mb-1 overflow-hidden rounded border {resultIsError ? 'border-accent2/40' : 'border-border'} bg-bg"
        >
          <div
            class="flex items-center justify-between gap-2 border-b {resultIsError ? 'border-accent2/40 bg-accent2/10 text-accent2' : 'border-border bg-panel2/50 text-muted'} px-2 py-0.5 text-[9px] uppercase tracking-wider"
          >
            <span class="flex items-center gap-1">
              {resultIsError ? 'OUT · error' : 'OUT'}
              {#if outputBackendTruncated && outputFullLen}
                <span class="rounded bg-warn/20 px-1 py-0.5 text-warn normal-case tracking-normal" title="Output truncado pelo backend ({fmtBytes(outputFullLen)} total) — clique em View full pra ver tudo">
                  truncated · {fmtBytes(outputFullLen)}
                </span>
              {/if}
            </span>
            <span class="flex items-center gap-1">
              {#if outputBackendTruncated && !fullOutput}
                <button
                  type="button"
                  onclick={loadFullOutput}
                  disabled={loadingFull}
                  class="inline-flex items-center gap-1 rounded px-1 py-0.5 text-[9px] hover:bg-panel2 hover:text-fg disabled:opacity-50"
                  title="Fetch full output from server"
                >
                  {#if loadingFull}
                    <Loader2 class="h-3 w-3 animate-spin" />
                    <span>loading…</span>
                  {:else}
                    <FileSearch class="h-3 w-3" />
                    <span>view full</span>
                  {/if}
                </button>
              {/if}
              <button
                type="button"
                onclick={toggleOutputExpanded}
                class="inline-flex items-center gap-1 rounded px-1 py-0.5 text-[9px] hover:bg-panel2 hover:text-fg"
                title={outputExpanded ? 'Collapse' : 'Expand (remove height limit)'}
                aria-label={outputExpanded ? 'Collapse output' : 'Expand output'}
              >
                {#if outputExpanded}
                  <Minimize2 class="h-3 w-3" />
                  <span>collapse</span>
                {:else}
                  <Maximize2 class="h-3 w-3" />
                  <span>expand</span>
                {/if}
              </button>
              <button
                type="button"
                onclick={copyOut}
                class="inline-flex items-center gap-1 rounded px-1 py-0.5 text-[9px] hover:bg-panel2 hover:text-fg"
                title={copiedOut ? 'Copied' : outputTruncated ? 'Copy full output' : 'Copy'}
                aria-label={copiedOut ? 'Copied' : 'Copy output'}
              >
                {#if copiedOut}
                  <Check class="h-3 w-3 text-ok" />
                  <span class="text-ok">copied</span>
                {:else}
                  <Copy class="h-3 w-3" />
                  <span>copy</span>
                {/if}
              </button>
            </span>
          </div>
          {#if fullError}
            <p class="px-2 py-1 text-[10px] text-accent2">{fullError}</p>
          {/if}
          <pre class="m-0 {outputExpanded ? '' : 'max-h-[400px]'} overflow-auto p-2 text-[11px] {resultIsError ? 'text-accent2' : 'text-fg/90'} whitespace-pre-wrap break-words">{outputDisplay}</pre>
        </div>
      {/if}
    {/if}
  </div>
{/if}
