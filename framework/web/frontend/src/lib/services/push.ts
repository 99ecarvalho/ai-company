/**
 * Web Push subscription helpers — registers /sw.js (scope /), wires
 * VAPID keys, and posts the subscription to the backend. Mirrors the
 * legacy enablePush/disablePush/togglePush flow.
 */
import { getPushConfig, subscribePush, unsubscribePush } from '$lib/api';

export type PushState = 'unsupported' | 'blocked' | 'disabled' | 'enabled';

export function pushSupported(): boolean {
  return (
    typeof window !== 'undefined' &&
    'Notification' in window &&
    'serviceWorker' in navigator &&
    'PushManager' in window
  );
}

export function urlB64ToUint8Array(b64: string): Uint8Array {
  const padding = '='.repeat((4 - (b64.length % 4)) % 4);
  const base64 = (b64 + padding).replace(/-/g, '+').replace(/_/g, '/');
  const raw = atob(base64);
  const a = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) a[i] = raw.charCodeAt(i);
  return a;
}

export async function registerServiceWorker(): Promise<ServiceWorkerRegistration | null> {
  if (!('serviceWorker' in navigator)) return null;
  try {
    const reg = await navigator.serviceWorker.register('/sw.js', { scope: '/' });
    return reg;
  } catch {
    return null;
  }
}

export async function getCurrentSubscription(): Promise<PushSubscription | null> {
  if (!pushSupported()) return null;
  try {
    const reg = await navigator.serviceWorker.ready;
    return await reg.pushManager.getSubscription();
  } catch {
    return null;
  }
}

export async function currentPushState(): Promise<PushState> {
  if (!pushSupported()) return 'unsupported';
  if (Notification.permission === 'denied') return 'blocked';
  const sub = await getCurrentSubscription();
  return sub ? 'enabled' : 'disabled';
}

export async function enablePush(): Promise<PushState> {
  if (!pushSupported()) return 'unsupported';
  const perm = await Notification.requestPermission();
  if (perm !== 'granted') {
    return Notification.permission === 'denied' ? 'blocked' : 'disabled';
  }
  const cfg = await getPushConfig();
  if (!cfg.public_key) {
    throw new Error('VAPID public key not available');
  }
  const reg = await navigator.serviceWorker.ready;
  let sub = await reg.pushManager.getSubscription();
  if (!sub) {
    const key = urlB64ToUint8Array(cfg.public_key);
    sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      // PushManager.subscribe expects BufferSource; Uint8Array satisfies it
      // at runtime even when TS narrows to ArrayBufferLike.
      applicationServerKey: key.buffer as ArrayBuffer
    });
  }
  await subscribePush(sub.toJSON());
  return 'enabled';
}

export async function disablePush(): Promise<PushState> {
  const sub = await getCurrentSubscription();
  if (!sub) return currentPushState();
  try {
    await unsubscribePush(sub.endpoint);
  } catch {
    /* keep going — also unsubscribe locally */
  }
  await sub.unsubscribe();
  return currentPushState();
}
