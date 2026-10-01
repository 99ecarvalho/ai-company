<script lang="ts">
  import { Send, Paperclip } from 'lucide-svelte';
  import { defaultStream, streams } from '$lib/stores/streams';
  import { captureAttachments, addAttachment, removeAttachment, clearAttachments, attachmentsToMarkdown } from '$lib/stores/attachments';
  import { logEvent, status } from '$lib/stores/ui';
  import { sending, tryBeginSend, endSend } from '$lib/stores/sending';
  import { postMessage, uploadFile } from '$lib/api';
  import { refreshConversations, convFilter } from '$lib/stores/conversations';
  import { showConvPanel } from '$lib/stores/ui';
  import AudioRecorder from './AudioRecorder.svelte';
  import AttachmentList from './AttachmentList.svelte';
  import MentionPicker from './MentionPicker.svelte';
  import { pasteImages } from '$lib/services/paste';
  import { dragDropFiles } from '$lib/services/dragdrop';
  import { markSent } from '$lib/stores/pendingSends';

  let text = $state('');
  let topic = $state('');
  let selectedStream = $state('');
  let fileInputEl: HTMLInputElement | null = $state(null);
  let textareaEl: HTMLTextAreaElement | null = $state(null);

  $effect(() => {
    if (!selectedStream && $defaultStream) selectedStream = $defaultStream;
  });

  async function send() {
    if (!tryBeginSend()) return;
    status.set('sending…');
    try {
      const items = $captureAttachments;
      const fullContent = text.trim() + attachmentsToMarkdown(items);
      const result = await postMessage({
        stream: selectedStream,
        topic: topic.trim() || null,
        content: fullContent
      });
      logEvent(`captured → ${result.topic}`, 'ok');
      const newId = `${result.stream}/${result.topic}`;
      markSent(newId);
      text = '';
      topic = '';
      clearAttachments('capture');
      status.set('ready');
      // A new conv is always 'active' — force the filter so it shows up in
      // the sidebar even when the user was on 'closed'. Without this, they
      // would think the capture was never sent (same sidebar + no switch).
      convFilter.set('active');
      // Navigate immediately — the /c/<id> page loads the conv directly via
      // the API. An async background refresh populates the sidebar.
      showConvPanel(newId);
      refreshConversations();
    } catch (e) {
      logEvent(`capture: ${e}`, 'err');
      status.set('send failed');
    } finally {
      endSend();
    }
  }

  async function uploadFromInput(file: File) {
    status.set('uploading…');
    try {
      const info = await uploadFile(file);
      addAttachment('capture', info);
      logEvent(`attached: ${info.path} (${Math.round(info.size / 1024)}KB)`, 'ok');
      status.set('ready');
    } catch (e) {
      logEvent(`upload: ${e}`, 'err');
      status.set('upload failed');
    }
  }

  function onAttachChange(e: Event) {
    const input = e.target as HTMLInputElement;
    const f = input.files && input.files[0];
    if (!f) return;
    uploadFromInput(f).finally(() => {
      input.value = '';
    });
  }

  let mentionPicker = $state<MentionPicker | null>(null);

  function onKeydown(e: KeyboardEvent) {
    if (mentionPicker?.onTextareaKeydown(e)) return;
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
      e.preventDefault();
      send();
    }
  }

  function onInput() {
    mentionPicker?.onTextareaInput();
  }

  function injectTranscribed(t: string) {
    const cur = text.trim();
    text = cur ? `${cur}\n${t}` : t;
    textareaEl?.focus();
  }

  const canSend = $derived(
    (!!text.trim() || $captureAttachments.length > 0) && !!selectedStream && !$sending
  );
</script>

<section
  class="flex h-full flex-col gap-3 p-4"
  use:dragDropFiles={{
    onUpload: (info) => addAttachment('capture', info),
    onError: (e) => logEvent(`drop upload: ${e}`, 'err'),
    onStart: () => status.set('uploading…'),
    onDone: () => status.set('ready')
  }}
>
  <div class="flex flex-col gap-2 xs:flex-row xs:flex-wrap xs:items-center">
    <select
      class="min-h-tap rounded-md border border-border bg-panel2 px-3 py-2 text-sm"
      bind:value={selectedStream}
      aria-label="Stream"
    >
      {#each $streams as s (s.id)}
        <option value={s.name}>{s.name}</option>
      {/each}
    </select>
    <input
      type="text"
      class="min-h-tap flex-1 rounded-md border border-border bg-panel2 px-3 py-2 text-sm xs:min-w-[200px]"
      placeholder="topic (auto-generated if empty)"
      bind:value={topic}
    />
  </div>

  <div class="relative flex-1">
    <textarea
      bind:this={textareaEl}
      bind:value={text}
      rows="6"
      class="h-full w-full resize-none rounded-md border border-border bg-panel2 p-3 text-sm leading-relaxed focus:border-accent focus:outline-none"
      placeholder="What's on your mind? (Ctrl/Cmd+Enter to send; @ to mention a file)"
      onkeydown={onKeydown}
      oninput={onInput}
      use:pasteImages={{
        onUpload: (info) => addAttachment('capture', info),
        onError: (e) => logEvent(`paste upload: ${e}`, 'err'),
        onStart: () => status.set('uploading…'),
        onDone: () => status.set('ready')
      }}
    ></textarea>
    <MentionPicker
      bind:this={mentionPicker}
      textarea={textareaEl}
      bind:value={text}
      onUpdate={(v) => (text = v)}
    />
  </div>

  <AttachmentList
    items={$captureAttachments}
    onRemove={(i) => removeAttachment('capture', i)}
  />

  <div class="flex flex-wrap items-center justify-between gap-2">
    <div class="flex items-center gap-2">
      <button
        type="button"
        class="inline-flex min-h-tap items-center gap-2 rounded-md border border-border bg-panel2 px-3 py-2 text-sm hover:bg-bg"
        onclick={() => fileInputEl?.click()}
        aria-label="Attach file"
        title="Attach file"
      >
        <Paperclip class="h-5 w-5" />
        <span class="hidden xs:inline">Attach</span>
      </button>
      <input
        bind:this={fileInputEl}
        type="file"
        class="hidden"
        onchange={onAttachChange}
      />
      <AudioRecorder onTranscribed={injectTranscribed} />
    </div>

    <button
      type="button"
      class="inline-flex min-h-tap items-center gap-2 rounded-md bg-accent px-4 py-2 text-sm font-semibold text-on-accent hover:bg-accent/90 disabled:cursor-not-allowed disabled:opacity-50"
      disabled={!canSend}
      onclick={send}
    >
      <Send class="h-5 w-5" />
      Send
    </button>
  </div>
</section>
