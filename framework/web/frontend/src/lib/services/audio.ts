/**
 * Audio recording controller — port of makeAudioCtrl from legacy app.js.
 * Records via MediaRecorder, draws FFT-128 32-bar waveform on a canvas,
 * stops returning the blob. UI integration (button, label) lives in the
 * Svelte component that owns this controller.
 */

export interface AudioCtrl {
  start: () => Promise<void>;
  stop: () => void;
  isRecording: () => boolean;
  destroy: () => void;
  bindCanvas: (canvas: HTMLCanvasElement | null) => void;
  onStop: (cb: (blob: Blob, mime: string) => void) => void;
}

export function makeAudioCtrl(): AudioCtrl {
  let mediaRecorder: MediaRecorder | null = null;
  let chunks: BlobPart[] = [];
  let audioCtx: AudioContext | null = null;
  let analyser: AnalyserNode | null = null;
  let srcNode: MediaStreamAudioSourceNode | null = null;
  let waveRaf: number | null = null;
  let activeStream: MediaStream | null = null;
  let canvas: HTMLCanvasElement | null = null;
  let ctx2d: CanvasRenderingContext2D | null = null;
  let onStopCb: ((blob: Blob, mime: string) => void) | null = null;

  function drawIdle() {
    if (!ctx2d || !canvas) return;
    ctx2d.clearRect(0, 0, canvas.width, canvas.height);
  }

  function drawWave() {
    if (!analyser || !ctx2d || !canvas) return;
    const bufLen = analyser.frequencyBinCount;
    const data = new Uint8Array(bufLen);
    analyser.getByteFrequencyData(data);
    const W = canvas.width;
    const H = canvas.height;
    ctx2d.clearRect(0, 0, W, H);
    const bars = 32;
    const step = Math.floor(bufLen / bars);
    const bw = (W - bars) / bars;
    for (let i = 0; i < bars; i++) {
      let sum = 0;
      for (let j = 0; j < step; j++) sum += data[i * step + j];
      const avg = sum / step;
      const h = Math.max(2, (avg / 255) * H);
      ctx2d.fillStyle = '#ef4444';
      ctx2d.fillRect(i * (bw + 1), (H - h) / 2, bw, h);
    }
    waveRaf = requestAnimationFrame(drawWave);
  }

  function bindCanvas(c: HTMLCanvasElement | null) {
    canvas = c;
    ctx2d = c ? c.getContext('2d') : null;
  }

  function onStop(cb: (blob: Blob, mime: string) => void) {
    onStopCb = cb;
  }

  async function start() {
    if (mediaRecorder && mediaRecorder.state === 'recording') return;
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    activeStream = stream;
    let mime = 'audio/webm;codecs=opus';
    if (!MediaRecorder.isTypeSupported(mime)) mime = 'audio/mp4';
    if (!MediaRecorder.isTypeSupported(mime)) mime = '';
    mediaRecorder = new MediaRecorder(stream, mime ? { mimeType: mime } : {});
    chunks = [];
    try {
      const Ctor =
        (window.AudioContext as typeof AudioContext) ||
        ((window as unknown as { webkitAudioContext: typeof AudioContext })
          .webkitAudioContext as typeof AudioContext);
      audioCtx = new Ctor();
      analyser = audioCtx.createAnalyser();
      analyser.fftSize = 128;
      srcNode = audioCtx.createMediaStreamSource(stream);
      srcNode.connect(analyser);
      drawWave();
    } catch {
      /* visual is nice-to-have */
    }
    mediaRecorder.ondataavailable = (e) => {
      if (e.data.size > 0) chunks.push(e.data);
    };
    mediaRecorder.onstop = () => {
      activeStream?.getTracks().forEach((t) => t.stop());
      activeStream = null;
      if (audioCtx) {
        try {
          audioCtx.close();
        } catch {
          /* noop */
        }
        audioCtx = null;
        analyser = null;
        srcNode = null;
      }
      if (waveRaf) {
        cancelAnimationFrame(waveRaf);
        waveRaf = null;
      }
      drawIdle();
      const finalMime = mediaRecorder?.mimeType || 'audio/webm';
      const blob = new Blob(chunks, { type: finalMime });
      chunks = [];
      onStopCb?.(blob, finalMime);
    };
    mediaRecorder.start();
  }

  function stop() {
    if (!mediaRecorder) return;
    if (mediaRecorder.state !== 'recording') return;
    mediaRecorder.stop();
  }

  function destroy() {
    stop();
    bindCanvas(null);
    onStopCb = null;
  }

  function isRecording() {
    return !!mediaRecorder && mediaRecorder.state === 'recording';
  }

  return { start, stop, isRecording, destroy, bindCanvas, onStop };
}

export function suggestedAudioFilename(mime: string): string {
  const ext = mime.includes('mp4') ? 'mp4' : mime.includes('ogg') ? 'ogg' : 'webm';
  return `voice-${new Date().toISOString().replace(/[:.]/g, '-')}.${ext}`;
}
