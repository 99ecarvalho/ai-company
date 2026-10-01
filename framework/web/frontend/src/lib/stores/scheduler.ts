import { writable, get } from 'svelte/store';
import { api } from '$lib/api';
import { createSSEClient, coalesceRefresh, type SSEClient } from '$lib/sse';
import { logEvent } from './ui';

export interface SchedulerJob {
  id: string;
  action: string | null;
  cron: string | null;
  params: Record<string, unknown>;
  next_run_time: string | null; // ISO 8601
  paused: boolean;
  last_fire_at: number | null; // unix seconds
  last_status: 'ok' | 'error' | null;
  last_error: string | null;
  last_duration_ms: number | null;
}

export interface SchedulerHealth {
  status: string;
  uptime_sec: number;
  jobs_registered: number;
  jobs_fired: number;
  last_fire_at: number | null;
}

export const schedulerJobs = writable<SchedulerJob[]>([]);
export const schedulerHealth = writable<SchedulerHealth | null>(null);
export const schedulerError = writable<string | null>(null);

let sseClient: SSEClient | null = null;
let inFlight = false;

export async function refreshScheduler(): Promise<void> {
  if (inFlight) return;
  inFlight = true;
  try {
    const [{ jobs }, health] = await Promise.all([
      api.get<{ jobs: SchedulerJob[] }>('/api/scheduler/jobs'),
      api.get<SchedulerHealth>('/api/scheduler/health')
    ]);
    schedulerJobs.set(jobs);
    schedulerHealth.set(health);
    schedulerError.set(null);
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    schedulerError.set(msg);
    logEvent(`refresh scheduler: ${msg}`, 'err');
  } finally {
    inFlight = false;
  }
}

const refreshTriggered = coalesceRefresh(refreshScheduler);

export function startSchedulerStream(): void {
  if (sseClient) return;
  refreshScheduler();
  sseClient = createSSEClient({
    url: '/api/scheduler/events',
    onMessage: () => refreshTriggered(),
    onOpen: () => refreshTriggered()
  });
}

export function stopSchedulerStream(): void {
  if (sseClient) {
    sseClient.close();
    sseClient = null;
  }
}

export async function runJobNow(jobId: string): Promise<void> {
  await api.post(`/api/scheduler/jobs/${encodeURIComponent(jobId)}/run`);
  // Immediate refresh to see last_fire_at update when the job finishes
  // (next poll, 5s).
  refreshScheduler();
}

export async function pauseJob(jobId: string): Promise<void> {
  await api.post(`/api/scheduler/jobs/${encodeURIComponent(jobId)}/pause`);
  // Optimistic: mark paused locally; the next poll overwrites it.
  schedulerJobs.update((jobs) =>
    jobs.map((j) => (j.id === jobId ? { ...j, paused: true, next_run_time: null } : j))
  );
  refreshScheduler();
}

export async function resumeJob(jobId: string): Promise<void> {
  await api.post(`/api/scheduler/jobs/${encodeURIComponent(jobId)}/resume`);
  refreshScheduler();
}

export function findSchedulerJob(id: string): SchedulerJob | undefined {
  return get(schedulerJobs).find((j) => j.id === id);
}

// ---------- Custom jobs (DB-backed) ----------

/** A single run = a single conversation (`<base>-<unix-ts>`). The scheduler
 *  spawns one per fire so each run is independently archivable/readable. */
export interface CustomJobRun {
  id: number;
  stream: string;
  topic: string;
  created_at: string | null;
  last_message_at: string | null;
  msg_count: number;
  archived: boolean;
}

export interface CustomJob {
  slug: string;
  cron: string;
  action: string;
  params: Record<string, unknown>;
  description: string | null;
  enabled: boolean;
  created_by: string | null;
  created_at: string | null;
  updated_at: string | null;
  /** Recent runs (max 100), ordered by last_message_at DESC. Only set
   *  when action=post_message. */
  runs?: CustomJobRun[];
}

export interface CustomJobInput {
  slug: string;
  cron: string;
  action: string;
  params: Record<string, unknown>;
  description?: string | null;
  enabled?: boolean;
}

export interface CustomJobPatch {
  cron?: string;
  params?: Record<string, unknown>;
  description?: string | null;
  enabled?: boolean;
}

export const listCustomJobs = () =>
  api.get<{ items: CustomJob[] }>('/api/scheduler/custom');

export const createCustomJob = (job: CustomJobInput) =>
  api.post<{ ok: boolean; slug: string }>('/api/scheduler/custom', job);

export const updateCustomJob = (slug: string, patch: CustomJobPatch) =>
  api.put<{ ok: boolean; slug: string }>(
    `/api/scheduler/custom/${encodeURIComponent(slug)}`,
    patch
  );

export const deleteCustomJob = (slug: string) =>
  api.del<{ ok: boolean; slug: string }>(
    `/api/scheduler/custom/${encodeURIComponent(slug)}`
  );

// ---------- Users (for the sender dropdown in the form) ----------

export interface UserOption {
  id: number;
  username: string;
  full_name: string;
  kind: 'human' | 'bot';
}

export const listUsers = () => api.get<UserOption[]>('/api/users');

// ---------- Native routines (overrides) ----------

export interface NativeRoutine {
  id: string;
  action: string;
  default_cron: string;
  cron_override: string | null;
  effective_cron: string;
  enabled: boolean;
  params: Record<string, unknown>;
  description: string;
}

export interface RoutinePatch {
  cron_override?: string | null;
  enabled?: boolean;
}

export const listRoutines = () =>
  api.get<{ items: NativeRoutine[] }>('/api/scheduler/routines');

export const updateRoutine = (id: string, patch: RoutinePatch) =>
  api.put<{ ok: boolean; id: string }>(
    `/api/scheduler/routines/${encodeURIComponent(id)}`,
    patch
  );

export const resetRoutine = (id: string) =>
  api.del<{ ok: boolean; id: string }>(
    `/api/scheduler/routines/${encodeURIComponent(id)}`
  );

/** Returns a string like "in 2h 14min" or "now" for a future ISO timestamp. */
export function fmtUntil(iso: string | null): string {
  if (!iso) return '—';
  const target = new Date(iso).getTime();
  if (isNaN(target)) return '—';
  const diff = Math.floor((target - Date.now()) / 1000);
  if (diff <= 0) return 'now';
  if (diff < 60) return `in ${diff}s`;
  if (diff < 3600) {
    const m = Math.floor(diff / 60);
    const s = diff % 60;
    return s > 0 && m < 5 ? `in ${m}min ${s}s` : `in ${m}min`;
  }
  if (diff < 86400) {
    const h = Math.floor(diff / 3600);
    const m = Math.floor((diff % 3600) / 60);
    return m > 0 ? `in ${h}h ${m}min` : `in ${h}h`;
  }
  const d = Math.floor(diff / 86400);
  const h = Math.floor((diff % 86400) / 3600);
  return h > 0 ? `in ${d}d ${h}h` : `in ${d}d`;
}
