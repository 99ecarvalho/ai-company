<script lang="ts">
  import { onDestroy } from 'svelte';
  import { Volume2, Loader2 } from 'lucide-svelte';
  import type { Message } from '$lib/api';
  import { fileDownloadUrl, synthesizeSpeech, ApiError } from '$lib/api';
  import { fmtClock } from '$lib/services/format';
  import { renderBotMessage } from '$lib/services/markdown';
  import { basename, isViewable } from '$lib/services/paths';
  import { fileViewerTarget, logEvent, openOverlay } from '$lib/stores/ui';

  interface Props {
    msg: Message;
  }
  let { msg }: Props = $props();

  const variant = $derived(msg.is_self ? 'self' : msg.is_bot ? 'bot' : 'other');
  const html = $derived(
    renderBotMessage(msg.content || '', msg.is_bot ? msg.sender : null)
  );
  const display = $derived(msg.sender_full_name || msg.sender || '');

  // TTS state
  let ttsLoading = $state(false);
  let ttsAudioUrl = $state<string | null>(null);
  let ttsAudioEl: HTMLAudioElement | null = $state(null);

  function onContentClick(e: MouseEvent) {
    const target = e.target as HTMLElement | null;
    const copyBtn = target?.closest?.('.codeCopyBtn') as HTMLButtonElement | null;
    if (copyBtn) {
      e.preventDefault();
      const pre = copyBtn.parentElement?.querySelector('pre');
      const text = pre?.textContent ?? '';
      if (!text) return;
      navigator.clipboard.writeText(text).then(
        () => {
          const prev = copyBtn.textContent;
          copyBtn.textContent = 'Copied';
          copyBtn.classList.add('codeCopyBtn--ok');
          setTimeout(() => {
            copyBtn.textContent = prev;
            copyBtn.classList.remove('codeCopyBtn--ok');
          }, 1200);
        },
        (err) => logEvent(`copy: ${err}`, 'err')
      );
      return;
    }
    const a = target?.closest?.('.fileLink') as HTMLAnchorElement | null;
    if (!a || !a.dataset.filePath) return;
    e.preventDefault();
    const path = a.dataset.filePath;
    // Regra: nunca fazer um redirect que tire o humano da conversa.
    // Diretorio → abre /files em nova aba (nao navega na aba atual).
    // File viewable → FileViewer overlay (modal inline).
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

  /** Strip HTML/markdown pra ler texto puro no TTS. */
  function plainText(htmlSrc: string): string {
    const tmp = document.createElement('div');
    tmp.innerHTML = htmlSrc;
    return (tmp.textContent || '').trim();
  }

  async function speak() {
    if (ttsLoading) return;
    if (ttsAudioUrl && ttsAudioEl) {
      // Replay
      ttsAudioEl.currentTime = 0;
      ttsAudioEl.play();
      return;
    }
    const text = plainText(html);
    if (!text) return;
    ttsLoading = true;
    try {
      ttsAudioUrl = await synthesizeSpeech(text);
    } catch (e) {
      const msg = e instanceof ApiError ? e.detail : String(e);
      logEvent(`tts: ${msg}`, 'err');
    } finally {
      ttsLoading = false;
    }
  }

  onDestroy(() => {
    if (ttsAudioUrl) {
      try { URL.revokeObjectURL(ttsAudioUrl); } catch { /* noop */ }
    }
  });
</script>

<!-- Bubble com convencao chat-style pra distinguir user vs agent: user
     alinha direita com background accent (azul) + canto inferior-direito
     reto; agente alinha esquerda com background neutro + borda + canto
     inferior-esquerdo reto. max-w-[85%] preserva legibilidade em mobile
     sem ocupar a largura inteira da coluna. -->
<div
  class="flex min-w-0 max-w-[85%] flex-col gap-1 rounded-2xl px-3 py-2 text-sm shadow-sm"
  class:self-end={variant === 'self'}
  class:rounded-br-sm={variant === 'self'}
  class:bg-accent={variant === 'self'}
  class:text-on-accent={variant === 'self'}
  class:self-start={variant !== 'self'}
  class:rounded-bl-sm={variant !== 'self'}
  class:border={variant !== 'self'}
  class:border-border={variant !== 'self'}
  class:bg-panel2={variant !== 'self'}
>
  <div class="flex items-baseline justify-between gap-2 text-xs">
    <strong class:text-on-accent={variant === 'self'} class:text-fg={variant !== 'self'}>
      {display}
    </strong>
    <div class="flex items-center gap-2">
      {#if msg.is_bot}
        <button
          type="button"
          onclick={speak}
          disabled={ttsLoading}
          title={ttsAudioUrl ? 'Replay' : 'Speak'}
          aria-label="Speak"
          class="rounded p-0.5 text-muted hover:text-fg disabled:opacity-50"
        >
          {#if ttsLoading}
            <Loader2 class="h-3 w-3 animate-spin" />
          {:else}
            <Volume2 class="h-3 w-3" />
          {/if}
        </button>
        {#if ttsAudioUrl}
          <!-- svelte-ignore a11y_media_has_caption -->
          <audio bind:this={ttsAudioEl} src={ttsAudioUrl} controls={false} autoplay></audio>
        {/if}
      {/if}
      <span class:opacity-70={true}>{fmtClock(msg.timestamp)}</span>
    </div>
  </div>
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <div
    class="mdBody min-w-0 max-w-full break-words"
    role="presentation"
    onclick={onContentClick}
  >
    {@html html}
  </div>
</div>

<!-- Estilos de markdown movidos pra app.css (.mdBody) — compartilhados
     entre MessageBubble (chat) e FileViewerOverlay (visualizador .md). -->
