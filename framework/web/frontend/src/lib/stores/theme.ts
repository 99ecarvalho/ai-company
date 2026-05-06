import { writable } from 'svelte/store';

export type Theme = 'dark' | 'light';

const KEY = 'agf-theme';

function initial(): Theme {
  if (typeof window === 'undefined') return 'dark';
  const saved = window.localStorage.getItem(KEY);
  if (saved === 'light' || saved === 'dark') return saved;
  // Default: respeita preferencia do SO; se nao tiver sinal, dark.
  if (window.matchMedia?.('(prefers-color-scheme: light)').matches) return 'light';
  return 'dark';
}

export const theme = writable<Theme>(initial());

export function applyTheme(t: Theme): void {
  if (typeof document !== 'undefined') {
    document.documentElement.dataset.theme = t;
  }
  if (typeof window !== 'undefined') {
    try {
      window.localStorage.setItem(KEY, t);
    } catch {
      /* ignora quota/disabled */
    }
  }
}

export function toggleTheme(): void {
  theme.update((t) => {
    const next: Theme = t === 'dark' ? 'light' : 'dark';
    applyTheme(next);
    return next;
  });
}
