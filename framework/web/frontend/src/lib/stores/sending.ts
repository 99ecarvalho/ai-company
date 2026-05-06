import { writable, get } from 'svelte/store';

export const sending = writable<boolean>(false);

/**
 * Guard a send operation against double-submit. Returns false if a send
 * is already in flight; returns true and flips the flag otherwise. Caller
 * must clear the flag in finally.
 */
export function tryBeginSend(): boolean {
  if (get(sending)) return false;
  sending.set(true);
  return true;
}

export function endSend() {
  sending.set(false);
}
