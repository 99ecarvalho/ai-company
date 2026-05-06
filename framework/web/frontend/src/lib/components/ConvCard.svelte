<script lang="ts">
  import {
    HelpCircle,
    MoreVertical,
    Archive,
    ArchiveRestore,
    ListChecks,
    Loader2,
    AlertTriangle,
    XCircle
  } from 'lucide-svelte';
  import type { ConversationSummary } from '$lib/api';
  import {
    deleteConversation as apiDelete,
    archiveConversation,
    setConversationTitle,
    unarchiveConversation
  } from '$lib/api';
  import { fmtAge } from '$lib/services/format';
  import { now } from '$lib/stores/clock';
  import {
    dropConversationLocally,
    patchConversationLocally,
    refreshConversations
  } from '$lib/stores/conversations';
  import { activeConvId, logEvent, showCapturePanel, showConvPanel } from '$lib/stores/ui';
  import { seen, forgetSeen } from '$lib/stores/seen';
  import { get } from 'svelte/store';

  interface Props {
    conv: ConversationSummary;
    /** Modo compacto pra filhos na arvore — so 1 linha com agent + sinais
     *  criticos + age. Titulo omite quando igual ao pai (herdado). Esconde
     *  badges de task metadata (workflow/step/complexity) e preview. */
    compact?: boolean;
    /** Titulo do pai (task.title || topic). Quando igual ao do filho, o
     *  titulo do filho e suprimido pra nao duplicar. So usado se compact. */
    parentTitle?: string | null;
  }
  let { conv, compact = false, parentTitle = null }: Props = $props();

  let active = $state(false);
  $effect(() => {
    const unsub = activeConvId.subscribe((v) => (active = v === conv.id));
    return () => unsub();
  });

  let menuOpen = $state(false);
  let menuRef: HTMLDivElement | null = $state(null);

  function onDocClick(e: MouseEvent) {
    if (!menuOpen) return;
    if (menuRef && !menuRef.contains(e.target as Node)) menuOpen = false;
  }
  $effect(() => {
    if (typeof document === 'undefined') return;
    document.addEventListener('click', onDocClick);
    return () => document.removeEventListener('click', onDocClick);
  });

  // Emoji shortcodes comuns usados pelos agentes na comunicacao via broker.
  // Subset do vocabulario Zulip/GitHub — suficiente pra previews limpos.
  const EMOJI_MAP: Record<string, string> = {
    loudspeaker: '📣',
    arrow_right: '→',
    white_check_mark: '✅',
    check: '✓',
    heavy_check_mark: '✔',
    x: '❌',
    warning: '⚠️',
    rocket: '🚀',
    tada: '🎉',
    question: '❓',
    bulb: '💡',
    zap: '⚡',
    fire: '🔥',
    hammer_and_wrench: '🛠️',
    wrench: '🔧',
    eyes: '👀',
    hourglass: '⏳',
    no_entry: '⛔',
    information_source: 'ℹ️',
    robot: '🤖',
    brain: '🧠',
    mag: '🔍',
    memo: '📝',
    clipboard: '📋',
    pushpin: '📌'
  };

  /** Limpa preview pra exibicao compacta: strip HTML, converte shortcodes
   *  de emoji conhecidos, remove markdown inline pesado (bold/italic/code)
   *  e colapsa whitespace. Mantem texto legivel. */
  function cleanPreview(s: string): string {
    if (!s) return '';
    let out = s;
    // strip HTML
    out = out.replace(/<[^>]+>/g, ' ');
    // emoji shortcodes :name:
    out = out.replace(/:([a-z0-9_+-]+):/gi, (_m, name: string) => {
      const key = name.toLowerCase();
      return EMOJI_MAP[key] ?? '';
    });
    // inline markdown
    out = out.replace(/\*\*(.+?)\*\*/g, '$1'); // bold
    out = out.replace(/(?<!\*)\*(?!\*)(.+?)\*(?!\*)/g, '$1'); // italic
    out = out.replace(/`([^`]+)`/g, '$1'); // code
    // blockquote / list markers at line-start
    out = out.replace(/^\s*[-*>]\s+/gm, '');
    // headings
    out = out.replace(/^\s*#{1,6}\s+/gm, '');
    // collapse whitespace
    out = out.replace(/\s+/g, ' ').trim();
    return out;
  }

  // Migration 027: humano pode override do titulo via PWA. Fallback:
  // custom_title -> task.title -> topic. Apaga override (volta pro fallback)
  // ao salvar string vazia.
  const fallbackTitle = $derived(conv.task?.title || conv.topic);
  const selfTitle = $derived(conv.custom_title || fallbackTitle);
  const showTitle = $derived(!compact || (parentTitle !== null && selfTitle !== parentTitle));

  // Inline edit state. Edit mode entra ao clicar no titulo; save em blur ou
  // Enter; cancela em Escape. stopPropagation evita disparar onCardClick.
  let editing = $state(false);
  let editValue = $state('');
  let titleInputEl: HTMLInputElement | null = $state(null);

  function startEditTitle(e: MouseEvent) {
    e.stopPropagation();
    editValue = selfTitle || '';
    editing = true;
    queueMicrotask(() => {
      titleInputEl?.focus();
      titleInputEl?.select();
    });
  }

  async function commitTitle() {
    if (!editing) return;
    editing = false;
    const trimmed = editValue.trim();
    // Se voltou pro fallback ou nao mudou, manda null/no-op respectivamente.
    const next: string | null = trimmed && trimmed !== fallbackTitle ? trimmed : null;
    if ((conv.custom_title || null) === next) return;
    try {
      await setConversationTitle(conv.id, next);
      patchConversationLocally(conv.id, { custom_title: next });
    } catch (err) {
      logEvent(`set title: ${err}`, 'err');
    }
  }

  function onTitleKeydown(e: KeyboardEvent) {
    if (e.key === 'Enter') {
      e.preventDefault();
      titleInputEl?.blur(); // dispara commit via on:blur
    } else if (e.key === 'Escape') {
      e.preventDefault();
      editing = false;
    }
  }
  const previewClean = $derived(cleanPreview(conv.last_msg?.content_preview || ''));
  const isBot = $derived(!!conv.last_msg?.is_bot);
  const isArchived = $derived(!!conv.archived_at);
  const hasTask = $derived(!!conv.task);

  // D-84: modelo unificado de estado proprio. Precedencia (so um vence):
  //   awaiting_human > is_stuck > is_running > is_errored > idle
  // Sinais ja vem mutuamente exclusivos do backend (derivacao em broker.py
  // aplica precedencia), mas por defesa o template tambem usa else-if.
  const awaitingHumanSelf = $derived(conv.awaiting_human === true);
  const isStuck = $derived(conv.is_stuck === true);
  const isRunning = $derived(conv.is_running === true);
  const isErrored = $derived(conv.is_errored === true);

  // D-96: ConvCard so renderiza pra raizes (filhas viraram chips no header).
  // Sinal askIsReplied removido — era para topics `__ask-from-*` que sumiram
  // da sidebar.

  // D-96: filha nao tem ask_human (gateado no MCP), entao a cascata
  // "NEEDS YOU ↓" do descendente saiu — `children_stats.awaiting_human` e
  // sempre 0. Mantemos so badges de "running" e "stuck" das filhas.
  const activeChildren = $derived(conv.children_stats?.active ?? 0);
  const stuckChildren = $derived(conv.children_stats?.stuck ?? 0);
  const runningChildren = $derived(Math.max(0, activeChildren - stuckChildren));
  const runningPulse = $derived(runningChildren > 0);

  // Unread discreto: ultima msg e de bot, sem awaiting_human (que domina
  // visualmente), nao e a conv ativa, e last_activity > lastSeen.
  const unread = $derived.by(() => {
    if (awaitingHumanSelf) return false;
    if (!isBot) return false;
    if (active) return false;
    const lastSeen = $seen[conv.id] ?? 0;
    return conv.last_activity > lastSeen;
  });

  function onCardClick() {
    showConvPanel(conv.id);
  }

  // Conta descendentes pra incluir no confirm dialog — somatorio de
  // active + resolved (ignora awaiting_human que e subset dos outros).
  const descendantCount = $derived(
    (conv.children_stats?.active ?? 0) + (conv.children_stats?.resolved ?? 0)
  );

  async function onArchiveClick(e: MouseEvent) {
    e.stopPropagation();
    menuOpen = false;
    if (
      descendantCount > 0 &&
      !confirm(
        `Archive "${conv.topic}" and ${descendantCount} sub-conversation${descendantCount === 1 ? '' : 's'} (cascade)?`
      )
    ) {
      return;
    }
    try {
      const res = await archiveConversation(conv.id);
      const n = res.cascaded_ids?.length ?? 0;
      logEvent(n > 0 ? `archived ${conv.id} (+${n} cascaded)` : `archived ${conv.id}`, 'ok');
      dropConversationLocally(conv.id);
      forgetSeen(conv.id);
      if (get(activeConvId) === conv.id) showCapturePanel();
      refreshConversations();
    } catch (err) {
      logEvent(`archive: ${err}`, 'err');
    }
  }

  async function onUnarchiveClick(e: MouseEvent) {
    e.stopPropagation();
    menuOpen = false;
    try {
      const res = await unarchiveConversation(conv.id);
      const n = res.cascaded_ids?.length ?? 0;
      logEvent(n > 0 ? `unarchived ${conv.id} (+${n} cascaded)` : `unarchived ${conv.id}`, 'ok');
      dropConversationLocally(conv.id);
      refreshConversations();
    } catch (err) {
      logEvent(`unarchive: ${err}`, 'err');
    }
  }

  async function onDeleteClick(e: MouseEvent) {
    e.stopPropagation();
    menuOpen = false;
    const warning =
      descendantCount > 0
        ? `Delete "${conv.topic}" AND ${descendantCount} sub-conversation${descendantCount === 1 ? '' : 's'} forever? All messages will be lost.`
        : `Delete "${conv.topic}" forever? All messages will be lost.`;
    if (!confirm(warning)) return;
    try {
      const res = await apiDelete(conv.id);
      const n = res.cascaded_ids?.length ?? 0;
      logEvent(n > 0 ? `deleted ${conv.id} (+${n} cascaded)` : `deleted ${conv.id}`, 'ok');
      dropConversationLocally(conv.id);
      forgetSeen(conv.id);
      if (get(activeConvId) === conv.id) showCapturePanel();
      refreshConversations();
    } catch (err) {
      logEvent(`delete: ${err}`, 'err');
    }
  }
</script>

<!-- svelte-ignore a11y_click_events_have_key_events -->
<div
  class="group relative cursor-pointer px-3 transition-colors hover:bg-panel2"
  class:py-1={compact}
  class:py-2={!compact}
  class:bg-panel2={active}
  class:border-l-2={active || awaitingHumanSelf}
  class:border-l-accent={active && !awaitingHumanSelf}
  class:border-l-warn={awaitingHumanSelf}
  data-id={conv.id}
  data-awaiting-human={awaitingHumanSelf ? 'true' : 'false'}
  data-running={isRunning ? 'true' : 'false'}
  data-stuck={isStuck ? 'true' : 'false'}
  data-errored={isErrored ? 'true' : 'false'}
  data-unread={unread ? 'true' : 'false'}
  data-archived={isArchived ? 'true' : 'false'}
  role="button"
  tabindex="0"
  onclick={onCardClick}
>
  {#if awaitingHumanSelf}
    <div class="pointer-events-none absolute inset-0 bg-warn/10" aria-hidden="true"></div>
  {/if}

  <div class="relative flex items-center gap-2 text-xs">
    {#if unread}
      <span
        class="h-2 w-2 shrink-0 rounded-full bg-accent"
        aria-label="Unread"
        title="New message"
      ></span>
    {/if}
    {#if runningPulse}
      <span
        class="relative flex h-2 w-2 shrink-0"
        aria-label="Child delegation running"
        title="Child delegation running"
      >
        <span class="absolute inline-flex h-full w-full animate-ping rounded-full bg-accent opacity-60"></span>
        <span class="relative inline-flex h-2 w-2 rounded-full bg-accent"></span>
      </span>
    {/if}
    {#if hasTask}
      <ListChecks class="h-3 w-3 shrink-0 text-accent" aria-label="Task attached" />
    {/if}
    <!-- D-84: 1 icone por card, precedencia
         awaiting_human(badge bottom) > stuck > running > errored > askReplied -->
    {#if isStuck}
      <AlertTriangle
        class="h-3 w-3 shrink-0 text-warn"
        aria-label="Stuck — turno aberto sem atividade"
      />
    {:else if isRunning}
      <Loader2
        class="h-3 w-3 shrink-0 animate-spin text-accent"
        aria-label="Agent is processing"
      />
    {:else if isErrored}
      <XCircle
        class="h-3 w-3 shrink-0 text-accent2"
        aria-label="Last turn errored"
      />
    {/if}
    <span
      class="flex-1 truncate font-semibold text-fg"
      class:font-bold={unread}
    >{conv.agent || '?'}</span>
    <span class="whitespace-nowrap text-muted">{fmtAge(conv.last_activity, $now)}</span>
    <div class="relative" bind:this={menuRef}>
      <button
        type="button"
        class="rounded p-0.5 text-muted hover:bg-bg hover:text-fg"
        onclick={(e) => {
          e.stopPropagation();
          menuOpen = !menuOpen;
        }}
        aria-label="Conversation menu"
        aria-haspopup="menu"
        aria-expanded={menuOpen}
        title="More"
      >
        <MoreVertical class="h-3.5 w-3.5" />
      </button>
      {#if menuOpen}
        <div
          role="menu"
          class="absolute right-0 top-full z-20 mt-1 min-w-[160px] rounded-md border border-border bg-panel shadow-lg"
        >
          {#if isArchived}
            <button
              type="button"
              role="menuitem"
              class="flex w-full items-center gap-2 px-2.5 py-1.5 text-left text-xs text-fg hover:bg-panel2"
              onclick={onUnarchiveClick}
            ><ArchiveRestore class="h-3 w-3" />Unarchive</button>
          {:else}
            <button
              type="button"
              role="menuitem"
              class="flex w-full items-center gap-2 px-2.5 py-1.5 text-left text-xs text-fg hover:bg-panel2"
              onclick={onArchiveClick}
            ><Archive class="h-3 w-3" />Archive</button>
          {/if}
          <button
            type="button"
            role="menuitem"
            class="block w-full px-2.5 py-1.5 text-left text-xs text-accent2 hover:bg-panel2"
            onclick={onDeleteClick}
          >Delete forever</button>
        </div>
      {/if}
    </div>
  </div>
  {#if showTitle}
    {#if editing}
      <!-- svelte-ignore a11y_autofocus -->
      <input
        bind:this={titleInputEl}
        bind:value={editValue}
        onclick={(e) => e.stopPropagation()}
        onblur={commitTitle}
        onkeydown={onTitleKeydown}
        type="text"
        maxlength="200"
        placeholder={fallbackTitle}
        aria-label="Edit conversation title"
        class="relative mt-0.5 w-full rounded-sm border border-accent bg-bg px-1 py-0.5 text-xs text-fg outline-none"
      />
    {:else}
      <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
      <div
        class="relative mt-0.5 truncate text-xs text-muted hover:text-fg"
        title={`${selfTitle} — click to rename`}
        onclick={startEditTitle}
        role="button"
        tabindex="-1"
      >
        {selfTitle}
      </div>
    {/if}
  {/if}
  {#if conv.task && !compact}
    <div class="relative mt-1 flex items-center gap-1.5 text-[10px] text-muted">
      {#if conv.task.workflow}
        <span class="rounded bg-panel2 px-1.5 py-0.5 font-mono">{conv.task.workflow}</span>
      {/if}
      {#if conv.task.current_step}
        <span>step: <span class="font-mono text-fg">{conv.task.current_step}</span></span>
      {/if}
      {#if conv.task.status !== 'in_progress'}
        <span class="rounded bg-panel2 px-1.5 py-0.5 uppercase">{conv.task.status}</span>
      {/if}
    </div>
  {/if}
  {#if previewClean && !conv.task && !compact}
    <div class="relative mt-1 line-clamp-2 text-xs text-muted">
      {#if !isBot}<em class="not-italic text-muted/70">you: </em>{/if}{previewClean}
    </div>
  {/if}
  {#if awaitingHumanSelf || activeChildren > 0 || stuckChildren > 0}
    <div class="relative mt-1 flex flex-wrap gap-1">
      {#if awaitingHumanSelf}
        <span
          class="inline-flex items-center gap-1 rounded bg-warn px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wider text-on-accent shadow-sm"
        >
          <HelpCircle class="h-3 w-3" />
          needs you
        </span>
      {/if}
      {#if stuckChildren > 0}
        <span
          class="inline-flex items-center gap-1 rounded-sm border border-warn/40 bg-warn/10 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-warn"
          title="{stuckChildren} descendant{stuckChildren === 1 ? '' : 's'} stuck"
        >
          <AlertTriangle class="h-3 w-3" />
          {stuckChildren} stuck
        </span>
      {/if}
      {#if runningChildren > 0}
        <span
          class="inline-flex items-center gap-1 rounded-sm border border-accent/30 bg-accent/10 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-accent"
          title="{runningChildren} delegation{runningChildren === 1 ? '' : 's'} active"
        >
          <Loader2 class="h-3 w-3 animate-spin" />
          {runningChildren} active
        </span>
      {/if}
    </div>
  {/if}
</div>
