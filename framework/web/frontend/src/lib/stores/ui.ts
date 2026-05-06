import { derived, writable, get } from 'svelte/store';
import { page } from '$app/stores';
import { goto } from '$app/navigation';
import { pushToast } from './toasts';

export type Panel = 'capture' | 'conv';
// Overlays residuais: só sub-interações que não são destino de navegação.
// Destinos (backlog, telemetry, memory, settings, files, search, log) são
// rotas SvelteKit (`/backlog`, etc).
export type OverlayId = 'hire' | 'fileViewer' | 'fileBrowser' | 'childConv';

/**
 * Panel ativo derivado da rota:
 * - '/c/<rest>' → 'conv'
 * - qualquer outra rota → 'capture'
 */
export const activePanel = derived<typeof page, Panel>(page, ($p) =>
  $p.url.pathname.startsWith('/c/') ? 'conv' : 'capture'
);

/**
 * ConvId ativo derivado do route param `convId` (rest param, pode conter '/').
 * null quando não estamos numa rota de conversa.
 */
export const activeConvId = derived<typeof page, string | null>(page, ($p) => {
  const raw = $p.params?.convId;
  return raw ? decodeURIComponent(raw) : null;
});

// Mobile-only: controla se em `/` mostramos a ConvListPane (true, home padrão)
// ou o main panel com CapturePanel (false, via FAB). No desktop não afeta nada:
// rail + ConvListPane + main são todos visíveis simultaneamente.
export const mobileShowList = writable<boolean>(true);

export const openOverlays = writable<Set<OverlayId>>(new Set());

// Drawer lateral (DrawerMenu) — controlado pela aba "More" do AppNav.
export const drawerOpen = writable<boolean>(false);

export interface FileViewerRequest {
  path: string;
}
export const fileViewerTarget = writable<FileViewerRequest | null>(null);

export interface FileBrowserRequest {
  path: string;
}
export const fileBrowserTarget = writable<FileBrowserRequest | null>(null);

/** Abre o browser de arquivos como overlay, preservando a rota atual. */
export function openFileBrowserAt(path: string) {
  fileBrowserTarget.set({ path });
  openOverlay('fileBrowser');
}

export interface ChildConvRequest {
  convId: string;
}
export const childConvTarget = writable<ChildConvRequest | null>(null);

/** Abre conversa filha (delegated) como overlay sem sair da conversa pai. */
export function openChildConv(convId: string) {
  childConvTarget.set({ convId });
  openOverlay('childConv');
}

export function openOverlay(id: OverlayId) {
  openOverlays.update((s) => {
    const n = new Set(s);
    n.add(id);
    return n;
  });
}

export function closeOverlay(id: OverlayId) {
  openOverlays.update((s) => {
    const n = new Set(s);
    n.delete(id);
    return n;
  });
}

export function closeAllOverlays() {
  openOverlays.set(new Set());
}

/** Navigates to the capture page (home). On mobile also hides the list. */
export function showCapturePanel() {
  mobileShowList.set(false);
  const current = get(page).url.pathname;
  if (current !== '/') goto('/');
}

/** Navigates to a conversation. On mobile also hides the list so the conv takes the screen. */
export function showConvPanel(convId: string) {
  mobileShowList.set(false);
  goto(`/c/${encodeURI(convId)}`);
}

/** Shows the conversation list on mobile (home state). No-op on desktop. */
export function showListOnMobile() {
  mobileShowList.set(true);
  const current = get(page).url.pathname;
  if (current !== '/') goto('/');
}

/** Opens the Files page deep-linked to a subpath. */
export function showFilesAt(path: string) {
  goto(`/files?path=${encodeURIComponent(path)}`);
}

// ---------- Log ----------

export interface LogEntry {
  ts: number;
  msg: string;
  cls: '' | 'ok' | 'err';
}

export const logEntries = writable<LogEntry[]>([]);

const LOG_CAP = 100;

export function logEvent(msg: string, cls: LogEntry['cls'] = '') {
  const ts = Date.now();
  logEntries.update((arr) => {
    const next = [{ ts, msg, cls }, ...arr];
    if (next.length > LOG_CAP) next.length = LOG_CAP;
    return next;
  });
  if (cls === 'err') {
    console.warn('[app]', msg);
    pushToast(msg);
  }
}

export function clearLog() {
  logEntries.set([]);
}

// ---------- Status (transient) ----------

export const status = writable<string>('ready');
