import { writable } from 'svelte/store';
import { listStreams, type StreamInfo } from '$lib/api';
import { logEvent } from './ui';

export const streams = writable<StreamInfo[]>([]);
export const defaultStream = writable<string>('');

export async function refreshStreams() {
  try {
    const { streams: list, default: def } = await listStreams();
    streams.set(list);
    defaultStream.set(def);
  } catch (e) {
    logEvent(`streams: ${e}`, 'err');
  }
}
