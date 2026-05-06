import { writable } from 'svelte/store';

export interface Toast {
  id: number;
  msg: string;
  ts: number;
}

export const toasts = writable<Toast[]>([]);

let nextId = 1;

export function pushToast(msg: string): number {
  const id = nextId++;
  toasts.update((arr) => [...arr, { id, msg, ts: Date.now() }]);
  return id;
}

export function dismissToast(id: number) {
  toasts.update((arr) => arr.filter((t) => t.id !== id));
}
