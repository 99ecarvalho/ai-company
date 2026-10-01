<script lang="ts">
  import { onDestroy } from 'svelte';
  import { Volume2, Loader2 } from 'lucide-svelte';
  import type { Message } from '$lib/api';
  import { fileDownloadUrl, synthesizeSpeech, ApiError } from '$lib/api';
  import { fmtClock } from '$lib/services/format';
  import { renderBotMessage, renderUserMessage } from '$lib/services/markdown';
  import { basename, isViewable } from '$lib/services/paths';
  import { fileViewerTarget, logEvent, openOverlay } from '$lib/stores/ui';
  import { currentUser } from '$lib/stores/auth';

  interface Props {
    msg: Message;
  }
  let { msg }: Props = $props();

  // The backend doesn't set is_self in the message payload — we derive it by
  // comparing sender with the logged-in user's username. Without this, the
  // human's messages fall into the 'other' variant and lose the highlight
  // (bg-accent + self-end).
  const isSelf = $derived(
    msg.is_self === true ||
      (!!$currentUser?.username && msg.sender === $currentUser.username)
  );
  const variant = $derived(isSelf ? 'self' : msg.is_bot ? 'bot' : 'other');
  // Bot keeps the CommonMark default (breaks: false) to preserve the LLM's
  // intentional markdown. Human uses renderUserMessage (breaks: true) — soft
  // breaks become <br>, the Slack-like behavior expected on paste.
  const html = $derived(
    msg.is_bot
      ? renderBotMessage(msg.content || '', msg.sender)
      : renderUserMessage(msg.content || '')
  );
  const display = $derived(msg.sender_full_name || msg.sender || '');

  // TTS state
  let ttsLoading = $state(false);
  let ttsAudioUrl = $state<string | null>(null);
  let ttsAudioEl: HTMLAudioElement | null = $state(null);

  let bodyEl: HTMLDivElement | null = $state(null);

  function onContentClick(e: MouseEvent) {
    const target = e.target as HTMLElement | null;
    const expandBtn = target?.closest?.('.codeExpandBtn') as HTMLButtonElement | null;
    if (expandBtn) {
      e.preventDefault();
      const wrapper = expandBtn.closest('.codeBlock') as HTMLElement | null;
      if (!wrapper) return;
      const expanded = wrapper.classList.toggle('codeBlock--expanded');
      expandBtn.setAttribute('aria-expanded', String(expanded));
      expandBtn.textContent = expanded ? 'Collapse' : 'Expand';
      expandBtn.setAttribute('title', expanded ? 'Collapse' : 'Expand');
      expandBtn.setAttribute(
        'aria-label',
        expanded ? 'Collapse code' : 'Expand code'
      );
      return;
    }
    const copyBtn = target?.closest?.('.codeCopyBtn') as HTMLButtonElement | null;
    if (copyBtn) {
      e.preventDefault();
      const pre = copyBtn.closest('.codeBlock')?.querySelector('pre');
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
    // Rule: never redirect in a way that takes the human out of the conversation.
    // Directory → opens /files in a new tab (doesn't navigate the current tab).
    // Viewable file → FileViewer overlay (inline modal).
    // Non-viewable file → download via anchor (stays on the current tab).
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

  /** Strip HTML/markdown to read plain text in TTS. */
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

  // Marks codeBlocks that exceed the height cap (CSS .mdBody .codeBlock pre
  // max-height) with data-expandable="true". The CSS uses that flag to show
  // the Expand button only when there's hidden content — short code blocks
  // don't get useless UI.
  $effect(() => {
    void html;
    if (!bodyEl) return;
    const blocks = bodyEl.querySelectorAll<HTMLElement>('.codeBlock');
    for (const blk of blocks) {
      const pre = blk.querySelector('pre') as HTMLPreElement | null;
      if (!pre) continue;
      const overflows = pre.scrollHeight > pre.clientHeight + 1;
      if (overflows) blk.setAttribute('data-expandable', 'true');
      else blk.removeAttribute('data-expandable');
    }
  });

  onDestroy(() => {
    if (ttsAudioUrl) {
      try { URL.revokeObjectURL(ttsAudioUrl); } catch { /* noop */ }
    }
  });
</script>

<!-- Chat-style bubble. User (self): right-aligned, bg-bubble-sent (soft
     accent tint, ~22% in dark / 15% in light — reads as "blue = sent"
     without the saturated peak), square bottom-right corner. Agent:
     left-aligned, bg-bubble-recv (neutral raised), square bottom-left
     corner. Text stays text-fg for both. max-w-[78%] guarantees ~22%
     empty space on the opposite side even for long msgs. -->
<div
  class="flex min-w-0 max-w-[78%] flex-col gap-1 rounded-2xl border px-3 py-2 text-sm text-fg shadow-sm"
  class:self-end={variant === 'self'}
  class:rounded-br-sm={variant === 'self'}
  class:bg-bubble-sent={variant === 'self'}
  class:border-bubble-sent-border={variant === 'self'}
  class:self-start={variant !== 'self'}
  class:rounded-bl-sm={variant !== 'self'}
  class:bg-bubble-recv={variant !== 'self'}
  class:border-bubble-recv-border={variant !== 'self'}
>
  <div class="flex items-baseline justify-between gap-2 text-xs">
    <strong class="text-fg">
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
    bind:this={bodyEl}
    class="mdBody min-w-0 max-w-full break-words"
    role="presentation"
    onclick={onContentClick}
  >
    {@html html}
  </div>
</div>

<!-- Markdown styles moved to app.css (.mdBody) — shared
     between MessageBubble (chat) and FileViewerOverlay (.md viewer). -->
