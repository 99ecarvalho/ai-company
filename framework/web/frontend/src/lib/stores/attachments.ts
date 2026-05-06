import { writable } from 'svelte/store';
import type { UploadResponse } from '$lib/api';

export type AttachKind = 'capture' | 'reply';

export const captureAttachments = writable<UploadResponse[]>([]);
export const replyAttachments = writable<UploadResponse[]>([]);

function storeFor(kind: AttachKind) {
  return kind === 'capture' ? captureAttachments : replyAttachments;
}

export function addAttachment(kind: AttachKind, info: UploadResponse) {
  storeFor(kind).update((arr) => [...arr, info]);
}

export function removeAttachment(kind: AttachKind, info: UploadResponse) {
  storeFor(kind).update((arr) => arr.filter((a) => a !== info));
}

export function clearAttachments(kind: AttachKind) {
  storeFor(kind).set([]);
}

export function attachmentsToMarkdown(items: UploadResponse[]): string {
  if (!items.length) return '';
  const lines = ['', '📎 **Attachments:**'];
  for (const a of items) lines.push(`- [${a.name}](${a.path})`);
  return '\n' + lines.join('\n');
}
