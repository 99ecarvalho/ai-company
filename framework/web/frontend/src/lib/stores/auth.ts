import { writable } from 'svelte/store';
import { authMe, type AuthMeResponse } from '$lib/api';

export const currentUser = writable<AuthMeResponse | null>(null);
export const authChecked = writable<boolean>(false);

export async function refreshAuth(): Promise<AuthMeResponse | null> {
  try {
    const me = await authMe();
    currentUser.set(me);
    return me;
  } catch {
    currentUser.set(null);
    return null;
  } finally {
    authChecked.set(true);
  }
}
