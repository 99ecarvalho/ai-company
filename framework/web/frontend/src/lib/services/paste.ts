/**
 * Svelte action — bind to a <textarea> to auto-upload pasted images.
 * Usage:
 *   <textarea use:pasteImages={{ onUpload }} />
 *   const onUpload = (info) => attachments.push(info);
 *
 * Only image-kind clipboard items are intercepted (text paste stays
 * default). File names are stamped with paste-<ISO>.<ext> to avoid
 * collisions across multiple pastes.
 */
import type { Action } from 'svelte/action';
import { uploadFile, type UploadResponse } from '$lib/api';

export interface PasteImagesOptions {
  onUpload?: (info: UploadResponse) => void;
  onError?: (err: unknown) => void;
  onStart?: () => void;
  onDone?: () => void;
}

export const pasteImages: Action<HTMLTextAreaElement, PasteImagesOptions> = (
  node,
  opts = {}
) => {
  let current = opts;

  async function handler(e: ClipboardEvent) {
    const items = e.clipboardData?.items;
    if (!items) return;
    const files: File[] = [];
    for (const it of items) {
      if (it.kind === 'file') {
        const f = it.getAsFile();
        if (f) files.push(f);
      }
    }
    if (!files.length) return;
    e.preventDefault();
    current.onStart?.();
    for (const f of files) {
      const ts = new Date().toISOString().replace(/[:.]/g, '-');
      const ext = (f.name.split('.').pop() || 'png').toLowerCase();
      const named = new File([f], `paste-${ts}.${ext}`, { type: f.type });
      try {
        const info = await uploadFile(named);
        current.onUpload?.(info);
      } catch (err) {
        current.onError?.(err);
      }
    }
    current.onDone?.();
  }

  node.addEventListener('paste', handler);
  return {
    update(newOpts: PasteImagesOptions) {
      current = newOpts;
    },
    destroy() {
      node.removeEventListener('paste', handler);
    }
  };
};
