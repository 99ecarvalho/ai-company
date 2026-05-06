/**
 * Svelte action — bind a um container (textarea wrapper, panel, etc) pra
 * aceitar drop de arquivos com auto-upload. Complementa pasteImages
 * (clipboard images) cobrindo qualquer tipo de arquivo.
 *
 * Uso:
 *   <div use:dragDropFiles={{ onUpload, onError }}>...</div>
 *
 * Adiciona class `drag-over` enquanto arquivos estao sendo arrastados
 * sobre o node — caller estiliza visualmente.
 */
import type { Action } from 'svelte/action';
import { uploadFile, type UploadResponse } from '$lib/api';

export interface DragDropFilesOptions {
  onUpload?: (info: UploadResponse) => void;
  onError?: (err: unknown) => void;
  onStart?: () => void;
  onDone?: () => void;
}

export const dragDropFiles: Action<HTMLElement, DragDropFilesOptions> = (
  node,
  opts = {}
) => {
  let current = opts;

  function preventDefault(e: DragEvent) {
    e.preventDefault();
    e.stopPropagation();
  }

  function hasFiles(e: DragEvent): boolean {
    const types = e.dataTransfer?.types;
    if (!types) return false;
    // DOMStringList vs Array
    for (let i = 0; i < types.length; i++) {
      if (types[i] === 'Files') return true;
    }
    return false;
  }

  async function onDrop(e: DragEvent) {
    preventDefault(e);
    node.classList.remove('drag-over');
    const files = Array.from(e.dataTransfer?.files || []);
    if (!files.length) return;
    current.onStart?.();
    for (const f of files) {
      try {
        const info = await uploadFile(f);
        current.onUpload?.(info);
      } catch (err) {
        current.onError?.(err);
      }
    }
    current.onDone?.();
  }

  function onDragEnter(e: DragEvent) {
    if (!hasFiles(e)) return;
    preventDefault(e);
    node.classList.add('drag-over');
  }

  function onDragOver(e: DragEvent) {
    if (!hasFiles(e)) return;
    preventDefault(e);
    if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy';
  }

  function onDragLeave(e: DragEvent) {
    if (!hasFiles(e)) return;
    preventDefault(e);
    // Soh remove se saimos do node de verdade (nao apenas pra um child).
    const related = e.relatedTarget as Node | null;
    if (!related || !node.contains(related)) {
      node.classList.remove('drag-over');
    }
  }

  node.addEventListener('dragenter', onDragEnter);
  node.addEventListener('dragover', onDragOver);
  node.addEventListener('dragleave', onDragLeave);
  node.addEventListener('drop', onDrop);

  return {
    update(newOpts: DragDropFilesOptions) {
      current = newOpts;
    },
    destroy() {
      node.removeEventListener('dragenter', onDragEnter);
      node.removeEventListener('dragover', onDragOver);
      node.removeEventListener('dragleave', onDragLeave);
      node.removeEventListener('drop', onDrop);
    }
  };
};
