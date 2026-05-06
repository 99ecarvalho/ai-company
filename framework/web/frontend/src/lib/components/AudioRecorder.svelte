<script lang="ts">
  import { onDestroy } from 'svelte';
  import { Mic, Square } from 'lucide-svelte';
  import { makeAudioCtrl, suggestedAudioFilename, type AudioCtrl } from '$lib/services/audio';
  import { transcribePreview } from '$lib/api';
  import { logEvent, status } from '$lib/stores/ui';

  interface Props {
    onTranscribed: (text: string) => void;
    /** Caller can probe to know if recording is active (e.g. dedup logic). */
    bindCtrl?: (ctrl: AudioCtrl) => void;
  }

  let { onTranscribed, bindCtrl }: Props = $props();

  let canvasEl: HTMLCanvasElement | null = $state(null);
  let recording = $state(false);
  const ctrl = makeAudioCtrl();
  // Surface the controller to parent so it can probe isRecording() etc.
  $effect(() => {
    bindCtrl?.(ctrl);
  });

  ctrl.onStop(async (blob, mime) => {
    recording = false;
    const filename = suggestedAudioFilename(mime);
    status.set('transcribing…');
    try {
      const data = await transcribePreview(blob, filename);
      const text = (data.text || '').trim();
      if (text) {
        onTranscribed(text);
        logEvent(
          `transcribed (${(data.audio_duration_sec || 0).toFixed(1)}s · ${data.language || '?'})`,
          'ok'
        );
      } else {
        logEvent('empty transcription', 'err');
      }
    } catch (e) {
      logEvent(`preview: ${e}`, 'err');
    }
    status.set('ready');
  });

  $effect(() => {
    if (canvasEl) ctrl.bindCanvas(canvasEl);
  });

  onDestroy(() => ctrl.destroy());

  async function toggle() {
    if (recording) {
      ctrl.stop();
    } else {
      try {
        await ctrl.start();
        recording = true;
        status.set('recording…');
      } catch (e) {
        logEvent(`mic: ${e}`, 'err');
      }
    }
  }
</script>

<div class="flex items-center gap-2">
  <button
    type="button"
    onclick={toggle}
    class="inline-flex min-h-tap items-center gap-2 rounded-md border border-border bg-panel2 px-3 py-2 text-sm hover:bg-bg"
    class:!bg-accent2={recording}
    class:!text-on-accent={recording}
    aria-pressed={recording}
    aria-label={recording ? 'Stop' : 'Record'}
  >
    {#if recording}
      <Square class="h-5 w-5" />
      <span class="hidden xs:inline">Stop</span>
    {:else}
      <Mic class="h-5 w-5" />
      <span class="hidden xs:inline">Record</span>
    {/if}
  </button>
  <canvas
    bind:this={canvasEl}
    width="220"
    height="32"
    class="hidden h-8 flex-1 rounded-md bg-bg xs:block xs:max-w-[220px]"
  ></canvas>
</div>
