/**
 * Overlay registry — só os overlays que permanecem como modal (sub-interações,
 * não "destinos"). Destinos viraram rotas SvelteKit (R2): /backlog, /telemetry,
 * /memory, /settings, /files, /search, /log.
 */
import type { Component } from 'svelte';
import type { OverlayId } from '$lib/stores/ui';

import HireOverlay from '$lib/components/overlays/HireOverlay.svelte';
import FileViewerOverlay from '$lib/components/overlays/FileViewerOverlay.svelte';
import FileBrowserOverlay from '$lib/components/overlays/FileBrowserOverlay.svelte';
import ChildConvOverlay from '$lib/components/overlays/ChildConvOverlay.svelte';

export const overlays: Partial<Record<OverlayId, Component>> = {
  hire: HireOverlay,
  fileViewer: FileViewerOverlay,
  fileBrowser: FileBrowserOverlay,
  childConv: ChildConvOverlay
};
