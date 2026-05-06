import { writable } from 'svelte/store';

/**
 * Mapa convId → unix-epoch (segundos) da ultima atividade vista pelo humano.
 * Persiste em localStorage. Usado pro badge "unread" discreto na sidebar
 * (complemento leve do badge forte de has_pending_ask).
 */
const KEY = 'agf-seen';

function load(): Record<string, number> {
  if (typeof window === 'undefined') return {};
  try {
    const raw = window.localStorage.getItem(KEY);
    return raw ? JSON.parse(raw) : {};
  } catch {
    return {};
  }
}

function persist(map: Record<string, number>) {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.setItem(KEY, JSON.stringify(map));
  } catch {
    /* quota/disabled — ignora */
  }
}

export const seen = writable<Record<string, number>>(load());

/** Marca conv como lida ate o timestamp informado (segundos). */
export function markSeen(id: string, lastActivity: number): void {
  seen.update((m) => {
    if ((m[id] ?? 0) >= lastActivity) return m;
    const next = { ...m, [id]: lastActivity };
    persist(next);
    return next;
  });
}

/** Remove do mapa — usado ao fechar conversa (limpa lixo). */
export function forgetSeen(id: string): void {
  seen.update((m) => {
    if (!(id in m)) return m;
    const { [id]: _, ...rest } = m;
    persist(rest);
    return rest;
  });
}
