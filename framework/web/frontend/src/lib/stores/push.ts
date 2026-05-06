import { get, writable } from 'svelte/store';
import {
  currentPushState,
  enablePush as enablePushSvc,
  disablePush as disablePushSvc,
  type PushState
} from '$lib/services/push';
import { logEvent } from './ui';

export const pushState = writable<PushState>('disabled');

export async function refreshPushState() {
  pushState.set(await currentPushState());
}

export async function enablePush() {
  try {
    pushState.set(await enablePushSvc());
    logEvent('push enabled', 'ok');
  } catch (e) {
    logEvent(`push: ${e}`, 'err');
    await refreshPushState();
  }
}

export async function disablePush() {
  try {
    pushState.set(await disablePushSvc());
    logEvent('push disabled', 'ok');
  } catch (e) {
    logEvent(`unsub: ${e}`, 'err');
    await refreshPushState();
  }
}

export async function togglePush() {
  const cur = get(pushState);
  if (cur === 'enabled') await disablePush();
  else if (cur !== 'unsupported' && cur !== 'blocked') await enablePush();
}
