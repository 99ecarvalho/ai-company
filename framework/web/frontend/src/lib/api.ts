/**
 * API contract — 1:1 with FastAPI backend (framework/web/app/main.py + broker.py + files.py).
 * Every shape here must match what the backend serializes.
 */

export interface ApiErrorBody {
  detail?: string;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public detail: string,
    public url: string
  ) {
    super(`${status} ${detail || 'request failed'} (${url})`);
    this.name = 'ApiError';
  }
}

/**
 * Hook called when an authenticated call returns 401. Default: redirect
 * to /login, unless we're already there or the call itself is login/me.
 * +layout.svelte can override it to coordinate with the auth store.
 */
export let onUnauthorized: (url: string) => void = (url: string) => {
  if (typeof window === 'undefined') return;
  const path = window.location.pathname;
  if (path === '/login') return;
  if (url.includes('/api/auth/login') || url.includes('/api/auth/me')) return;
  window.location.assign('/login');
};

export function setOnUnauthorized(cb: typeof onUnauthorized) {
  onUnauthorized = cb;
}

async function req<T>(
  method: string,
  url: string,
  opts: { body?: unknown; form?: FormData; signal?: AbortSignal } = {}
): Promise<T> {
  const init: RequestInit = {
    method,
    signal: opts.signal,
    credentials: 'same-origin'
  };
  const headers: Record<string, string> = {};
  if (opts.body !== undefined) {
    headers['content-type'] = 'application/json';
    init.body = JSON.stringify(opts.body);
  } else if (opts.form) {
    init.body = opts.form;
  }
  init.headers = headers;
  const r = await fetch(url, init);
  if (!r.ok) {
    let detail = '';
    try {
      const data = (await r.json()) as ApiErrorBody;
      detail = data.detail || JSON.stringify(data);
    } catch {
      try {
        detail = await r.text();
      } catch {
        detail = r.statusText;
      }
    }
    if (r.status === 401) onUnauthorized(url);
    throw new ApiError(r.status, detail, url);
  }
  if (r.status === 204) return undefined as T;
  const ct = r.headers.get('content-type') || '';
  if (ct.includes('application/json')) {
    return (await r.json()) as T;
  }
  return (await r.text()) as unknown as T;
}

export const api = {
  get: <T>(url: string, signal?: AbortSignal) => req<T>('GET', url, { signal }),
  post: <T>(url: string, body?: unknown, signal?: AbortSignal) =>
    req<T>('POST', url, { body, signal }),
  postForm: <T>(url: string, form: FormData, signal?: AbortSignal) =>
    req<T>('POST', url, { form, signal }),
  put: <T>(url: string, body?: unknown, signal?: AbortSignal) =>
    req<T>('PUT', url, { body, signal }),
  patch: <T>(url: string, body?: unknown, signal?: AbortSignal) =>
    req<T>('PATCH', url, { body, signal }),
  del: <T>(url: string, signal?: AbortSignal) => req<T>('DELETE', url, { signal })
};

// ---------- Conversations ----------

export interface LastMsg {
  sender: string | null;
  is_bot: boolean;
  content_preview: string;
  content: string;
  timestamp: number;
}

export interface ConversationTaskRef {
  slug: string;
  title: string | null;
  workflow: string | null;
  status: string;
  current_step: string | null;
  current_agent: string | null;
  complexity: string | null;
}

export interface ChildrenStats {
  /** D-96: children (1 level) that are still active (running/stuck/awaiting). */
  active: number;
  /** D-96: children with is_stuck=true (open turn with no activity). */
  stuck: number;
  /** D-96: children that finished their turn (idle, terminal task status, etc). */
  resolved: number;
}

export interface ConversationSummary {
  id: string; // "stream/topic"
  db_id: number;
  agent: string;
  stream: string;
  topic: string;
  /** Migration 027: title customizable by the human. `null` = no override
   *  (frontend fallback: `custom_title ?? task?.title ?? topic`). */
  custom_title: string | null;
  last_activity: number;
  last_message_at: string;
  archived_at: string | null;
  msg_count: number;
  last_msg: LastMsg;
  /** D-84 (formerly `has_pending_ask`): agent blocked in `ask_human` waiting
   *  for the human's answer. Absolute precedence over is_running/is_stuck. */
  awaiting_human: boolean;
  participating: boolean;
  closed: boolean;
  /** db_id of parent conversation (D-96: only one level deep); null = root. */
  parent_conv_id: number | null;
  children_stats: ChildrenStats;
  /** D-84: turn in flight (run_start without run_end) and activity within the
   *  stuck threshold. Excludes awaiting_human and is_stuck. */
  is_running: boolean;
  /** D-84: open turn with no activity for > RUNNER_STUCK_SEC. Same
   *  semantics as the header's `state=stuck` — parity between sidebar and header. */
  is_stuck: boolean;
  /** D-84: last turn ended in error (run_end with subtype != success)
   *  and there is no newer turn. Wins only if there is no awaiting_human. */
  is_errored: boolean;
  /** D-79: for `task-<slug>` conversations in secondary streams — true when
   *  the task has moved on and `current_agent != this.stream`. Signals the
   *  intermediate conv already finished its phase; the store treats it as
   *  resolved so children_stats.active doesn't inflate. */
  task_not_current_agent: boolean;
  task?: ConversationTaskRef;
}

export interface ConversationListResponse {
  items: ConversationSummary[];
}

export type ConvFilter = 'active' | 'closed';

export interface Message {
  id: number;
  sender: string;
  sender_full_name?: string;
  is_bot: boolean;
  is_self?: boolean;
  content: string;
  timestamp: number; // seconds
}

// D-71/D-84: state derived from the runner (from telemetry.live_events + pending_asks).
// Model unified with the sidebar — same names and precedence.
export type RunnerState =
  | 'idle'
  | 'running'
  | 'stuck'
  | 'awaiting_human'
  | 'errored';

export interface RunnerStateInfo {
  state: RunnerState;
  since: string | null; // ISO timestamp
  last_error: string | null;
  can_retry: boolean;
  can_cancel: boolean;
  stuck: boolean;
}

export interface ConversationDetail {
  id: string;
  agent: string;
  stream: string;
  topic: string;
  /** Migration 027: title customizable by the human. Same fallback as the summary. */
  custom_title: string | null;
  pending_ask_id: number | null;
  runner_state?: RunnerStateInfo;
  /** D-96: db_id of parent conversation (1 level deep); null = root. */
  parent_conv_id: number | null;
  messages: Message[];
}

export const listConversations = (filter: ConvFilter = 'active') =>
  api.get<ConversationListResponse>(`/api/conversations?filter=${filter}`);

export const deleteConversation = (convId: string) =>
  api.del<{ ok: boolean; cascaded_ids: number[] }>(
    `/api/conversations/${encodeConvId(convId)}`
  );

/** Migration 027: sets or clears (by passing null/'') the conversation's
 *  custom title. Display rule: `custom_title ?? task?.title ?? topic`. */
export const setConversationTitle = (convId: string, customTitle: string | null) =>
  api.patch<{ ok: boolean; id: number; custom_title: string | null }>(
    `/api/conversations/${encodeConvId(convId)}`,
    { custom_title: customTitle }
  );

export const archiveConversation = (convId: string) =>
  api.post<{ ok: boolean; archived: boolean; cascaded_ids: number[] }>(
    `/api/conversations/${encodeConvId(convId)}/archive`
  );

export const unarchiveConversation = (convId: string) =>
  api.post<{ ok: boolean; archived: boolean; cascaded_ids: number[] }>(
    `/api/conversations/${encodeConvId(convId)}/unarchive`
  );

// D-71: action endpoints + standalone fetch of runner_state.
export const getRunnerState = (convId: string) =>
  api.get<RunnerStateInfo>(
    `/api/conversations/${encodeConvId(convId)}/runner-state`
  );

export const cancelConversation = (convId: string) =>
  api.post<{ ok: boolean }>(`/api/conversations/${encodeConvId(convId)}/cancel`);

export const retryConversation = (convId: string) =>
  api.post<{ ok: boolean; dispatched: boolean; message_id: number }>(
    `/api/conversations/${encodeConvId(convId)}/retry`
  );

// ---------- Workflows (D-57) ----------

/** Overrides applied by claude_runner while the agent is executing
 *  this step. Workflow is authoritative: a missing field = inherits the
 *  agent's config from agents.yaml. */
export interface WorkflowStepOverrides {
  model?: string;
  effort?: 'low' | 'medium' | 'high' | 'xhigh' | 'max';
  memory?: {
    enabled?: boolean;
    auto_inject_limit?: number;
  };
}

export interface WorkflowStep {
  agent: string | null;
  artifact: string | null;
  next: string[];
  instructions?: string | null;
  overrides?: WorkflowStepOverrides | null;
}

export interface WorkflowDef {
  name: string;
  initial_step: string;
  /** D-110: agent that orchestrates the workflow — owner of the supervisor conv.
   *  When promoting via Path A (next_agent != orchestrator), the supervisor
   *  lives in <orchestrator>/task-<slug> and the initial phase becomes a child. */
  orchestrator?: string | null;
  steps_ordered: string[];
  steps: Record<string, WorkflowStep>;
  terminals: string[];
  description?: string | null;
}

export const getWorkflow = (name: string) =>
  api.get<WorkflowDef>(`/api/workflows/${encodeURIComponent(name)}`);

export const listWorkflowsExpanded = () =>
  api.get<{ items: WorkflowDef[]; terminals: string[]; expanded: true }>(
    '/api/workflows?expand=1'
  );

export interface WorkflowUpsertBody {
  initial_step: string;
  /** D-110: optional. Without it, the framework falls back to initial_step.agent. */
  orchestrator?: string | null;
  steps: Record<
    string,
    {
      agent: string | null;
      artifact: string | null;
      next: string[];
      instructions?: string | null;
      overrides?: WorkflowStepOverrides | null;
    }
  >;
  description?: string | null;
}

export const upsertWorkflow = (name: string, body: WorkflowUpsertBody) =>
  api.put<WorkflowDef>(`/api/workflows/${encodeURIComponent(name)}`, body);

export const deleteWorkflow = (name: string) =>
  api.del<{ ok: true }>(`/api/workflows/${encodeURIComponent(name)}`);

export const renameWorkflow = (name: string, new_name: string) =>
  api.post<WorkflowDef>(
    `/api/workflows/${encodeURIComponent(name)}/rename`,
    { new_name }
  );

export interface TaskStats {
  slug: string;
  runs: number;
  cost_usd: number;
  duration_ms: number;
  turns: number;
  conversations: number;
}

export const getTaskStats = (slug: string) =>
  api.get<TaskStats>(`/api/tasks/${encodeURIComponent(slug)}/stats`);

export const getConversationMessages = (convId: string) =>
  api.get<ConversationDetail>(
    `/api/conversations/${encodeConvId(convId)}/messages`
  );

export const closeConversation = (convId: string) =>
  api.post<{ ok: boolean }>(
    `/api/conversations/${encodeConvId(convId)}/close`
  );

export const closeAllConversations = () =>
  api.post<{ ok: boolean; result: string }>('/api/conversations/close-all');

function encodeConvId(id: string): string {
  // id is "stream/topic". Encode each segment but keep the slash.
  const i = id.indexOf('/');
  if (i < 0) return encodeURIComponent(id);
  return `${encodeURIComponent(id.slice(0, i))}/${encodeURIComponent(id.slice(i + 1))}`;
}

// ---------- Post message ----------

export interface PostMessageRequest {
  stream: string;
  topic: string | null;
  content: string;
  client_id?: string;
}

export interface PostMessageResponse {
  id: number;
  stream: string;
  topic: string;
  conversation_id: number;
  content: string;
  sent_at: string;
}

export const postMessage = (payload: PostMessageRequest) =>
  api.post<PostMessageResponse>('/api/post-message', payload);

// ---------- Streams ----------

export interface StreamInfo {
  id: number;
  name: string;
  description?: string | null;
  is_active?: boolean;
}

export interface StreamsResponse {
  streams: StreamInfo[];
  default: string;
}

export const listStreams = () => api.get<StreamsResponse>('/api/streams');

// ---------- Transcribe ----------

export interface TranscribeResponse {
  text: string;
  language?: string;
  audio_duration_sec?: number;
  [k: string]: unknown;
}

export const transcribePreview = (file: Blob, filename: string, language?: string) => {
  const fd = new FormData();
  fd.append('file', file, filename);
  if (language) fd.append('language', language);
  return api.postForm<TranscribeResponse>('/api/transcribe-preview', fd);
};

// ---------- TTS ----------

/**
 * Synthesize speech via backend proxy. Returns a blob URL ready for
 * <audio src=...>. The caller must revokeObjectURL when discarding it.
 */
export async function synthesizeSpeech(text: string): Promise<string> {
  // MP3 is ~5x smaller than WAV for the same speech; every target browser plays it.
  const r = await fetch('/api/tts/synthesize', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify({ text, format: 'mp3' })
  });
  if (!r.ok) {
    const body = await r.text().catch(() => '');
    let detail = body;
    try {
      const parsed = JSON.parse(body);
      if (typeof parsed?.detail === 'string') detail = parsed.detail;
    } catch {
      // not JSON: keep the raw body
    }
    throw new ApiError(r.status, detail, '/api/tts/synthesize');
  }
  const blob = await r.blob();
  return URL.createObjectURL(blob);
}

// ---------- Files ----------

export interface FileEntry {
  name: string;
  path: string;
  is_dir: boolean;
  size: number | null;
}

export interface FileListResponse {
  path: string;
  entries: FileEntry[];
}

export interface UploadResponse {
  path: string;
  name: string;
  size: number;
  mime: string;
}

export interface FileReadText {
  kind: 'text';
  path: string;
  mime: string;
  size: number;
  text: string;
}

export const listFiles = (path: string, showHidden = false) => {
  const qs = showHidden ? `&show_hidden=true` : '';
  return api.get<FileListResponse>(`/api/files/list?path=${encodeURIComponent(path)}${qs}`);
};

export const uploadFile = (file: File) => {
  const fd = new FormData();
  fd.append('file', file);
  return api.postForm<UploadResponse>('/api/files/upload', fd);
};

export interface WriteFileResponse {
  ok: boolean;
  path: string;
  size: number;
}

export const writeFile = (path: string, content: string) =>
  api.post<WriteFileResponse>('/api/files/write', { path, content });

export const fileReadUrl = (path: string) =>
  `/api/files/read?path=${encodeURIComponent(path)}`;

export const fileDownloadUrl = (path: string) =>
  `/api/files/read?path=${encodeURIComponent(path)}&raw=1`;

export interface FileSearchEntry {
  path: string;
  is_dir: boolean;
}

export interface FileSearchResponse {
  q: string;
  bases: string[];
  count: number;
  entries: FileSearchEntry[];
}

export const searchFiles = (q: string, opts?: { bases?: string; limit?: number }) => {
  const params = new URLSearchParams({ q });
  if (opts?.bases) params.set('bases', opts.bases);
  if (opts?.limit != null) params.set('limit', String(opts.limit));
  return api.get<FileSearchResponse>(`/api/files/search?${params.toString()}`);
};

export interface FileReadResult {
  kind: 'text' | 'binary' | 'image';
  text?: string;
  blobUrl?: string;
  mime: string;
  path: string;
}

export async function readFile(path: string): Promise<FileReadResult> {
  const url = fileReadUrl(path);
  const r = await fetch(url);
  if (!r.ok) throw new ApiError(r.status, await r.text().catch(() => ''), url);
  const ct = r.headers.get('content-type') || '';
  if (ct.includes('application/json')) {
    const data = (await r.json()) as FileReadText | { kind: 'binary'; mime: string; path: string };
    if (data.kind === 'text') {
      return { kind: 'text', text: data.text, mime: data.mime, path: data.path };
    }
    return { kind: 'binary', mime: data.mime, path };
  }
  if (ct.startsWith('image/')) {
    const blob = await r.blob();
    return { kind: 'image', blobUrl: URL.createObjectURL(blob), mime: ct, path };
  }
  return { kind: 'binary', mime: ct, path };
}

// ---------- App settings (web.app_settings) ----------

export interface WebSettingsGeneral {
  default_stream: string;
  vapid: {
    configured: boolean;
    public_key: string;
    contact_email: string;
  };
}

export const getWebSettings = () =>
  api.get<WebSettingsGeneral>('/api/web-settings/general');

export const updateWebSettings = (
  patch: Partial<{ default_stream: string; vapid_contact_email: string }>
) => api.put<WebSettingsGeneral>('/api/web-settings/general', patch);

export const generateVapid = (params?: { force?: boolean; contact_email?: string }) =>
  api.post<{ ok: boolean; public_key: string; subscriptions_invalidated: number }>(
    '/api/web-settings/vapid/generate',
    params ?? {}
  );

export const clearVapid = () =>
  api.del<{ ok: boolean; subscriptions_invalidated: number }>('/api/web-settings/vapid');

// ---------- Push ----------

export interface PushConfigResponse {
  enabled: boolean;
  public_key: string;
}

export const getPushConfig = () => api.get<PushConfigResponse>('/api/push-config');

export const subscribePush = (sub: PushSubscriptionJSON) =>
  api.post<{ ok: boolean }>('/api/push/subscribe', sub);

export const unsubscribePush = (endpoint: string) =>
  api.post<{ ok: boolean }>('/api/push/unsubscribe', { endpoint });

export const testPush = (delaySeconds = 0) =>
  api.post<{ ok: boolean; scheduled_in?: number; sent?: number; total?: number }>(
    `/api/push/test?delay_seconds=${encodeURIComponent(delaySeconds)}`
  );

// ---------- Telemetry ----------

export interface TelemetryTotals {
  runs?: number;
  total_cost_usd?: number;
  total_duration_ms?: number;
  input_tokens?: number;
  output_tokens?: number;
}

export interface TelemetryAgentRow {
  agent: string;
  runs: number;
  cost_usd: number;
  avg_duration_ms: number;
  input_tokens: number;
  output_tokens: number;
  cache_tokens: number;
  failures: number;
}

export interface TelemetryModelRow {
  model: string;
  runs: number;
  cost_usd: number;
  avg_duration_ms: number;
  input_tokens: number;
  output_tokens: number;
  cache_tokens: number;
  failures: number;
}

export interface TelemetrySummary {
  window: string;
  filters: { agent: string | null; model: string | null };
  totals: TelemetryTotals;
  by_agent: TelemetryAgentRow[];
  by_model: TelemetryModelRow[];
}

export interface TelemetryRecentRow {
  id: number;
  agent: string;
  topic_slug: string | null;
  event_type: string;
  cost_usd: number;
  input_tokens: number | null;
  output_tokens: number | null;
  duration_ms: number | null;
  model: string | null;
  started_at: number;
  ts: string;
  metadata: Record<string, unknown>;
  num_turns?: number;
  exit_code?: number;
  total_cost_usd?: number;
  topic?: string;
}

export interface TelemetryRecentResponse {
  items: TelemetryRecentRow[];
}

function buildTelemetryQuery(
  base: Record<string, string | number | null | undefined>
): string {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(base)) {
    if (v === null || v === undefined || v === '') continue;
    qs.set(k, String(v));
  }
  const s = qs.toString();
  return s ? `?${s}` : '';
}

export const telemetrySummary = (
  window: string,
  opts: { agent?: string | null; model?: string | null } = {}
) =>
  api.get<TelemetrySummary>(
    `/api/telemetry/summary${buildTelemetryQuery({ window, agent: opts.agent, model: opts.model })}`
  );

export const telemetryRecent = (
  limit = 20,
  opts: { agent?: string | null; model?: string | null } = {}
) =>
  api.get<TelemetryRecentResponse>(
    `/api/telemetry/recent${buildTelemetryQuery({ limit, agent: opts.agent, model: opts.model })}`
  );

export interface TelemetryTimeseriesPoint {
  ts: number;
  runs: number;
  cost_usd: number;
  duration_ms: number;
  output_tokens: number;
}

export interface TelemetryTimeseriesResponse {
  window: string;
  bucket: string;
  points: TelemetryTimeseriesPoint[];
}

export const telemetryTimeseries = (
  window: string,
  opts: { agent?: string | null; model?: string | null } = {}
) =>
  api.get<TelemetryTimeseriesResponse>(
    `/api/telemetry/timeseries${buildTelemetryQuery({ window, agent: opts.agent, model: opts.model })}`
  );

// ---------- Task telemetry (B4) ----------

export interface TaskTelemetryTotals {
  runs: number;
  cost_usd: number;
  duration_ms: number;
  input_tokens: number;
  output_tokens: number;
  cache_tokens: number;
  last_run_ts: number | null;
}

export interface TaskTelemetryBreakdownRow {
  model?: string;
  agent?: string;
  runs: number;
  cost_usd: number;
  avg_duration_ms: number;
}

export interface TaskTelemetrySnapshot {
  thinking_count: number;
  tool_use_count: number;
  snapshots: number;
}

export interface TaskTelemetryResponse {
  slug: string;
  totals: TaskTelemetryTotals;
  by_model: TaskTelemetryBreakdownRow[];
  by_agent: TaskTelemetryBreakdownRow[];
  trace_snapshot: TaskTelemetrySnapshot;
}

export const taskTelemetry = (slug: string) =>
  api.get<TaskTelemetryResponse>(
    `/api/tasks/${encodeURIComponent(slug)}/telemetry`
  );

// ---------- Cost budgets ----------

export interface CostBudgetRow {
  agent: string;
  daily_usd_limit: number;
  alert_message: string | null;
  spent_today_usd: number;
  pct: number;
  over: boolean;
}

export const listCostBudgets = () =>
  api.get<{ items: CostBudgetRow[] }>('/api/cost-budgets');

export const upsertCostBudget = (
  agent: string,
  daily_usd_limit: number,
  alert_message?: string | null
) => api.post<{ ok: boolean }>('/api/cost-budgets', { agent, daily_usd_limit, alert_message });

export const deleteCostBudget = (agent: string) =>
  api.del<{ ok: boolean }>(`/api/cost-budgets/${encodeURIComponent(agent)}`);

// ---------- Memory ----------

export interface MemoryAgent {
  agent: string;
  count: number;
}

export interface MemoryFact {
  id: number;
  key: string;
  value: string;
  tags: string[];
  created_at: number;
  updated_at: number;
}

export interface MemoryAgentsResponse {
  items: MemoryAgent[];
}

export interface MemoryFactsResponse {
  items: MemoryFact[];
  count: number;
}

export const listMemoryAgents = () =>
  api.get<MemoryAgentsResponse>('/api/memory/agents');

export const listMemoryFacts = (agent: string, q?: string, tag?: string) => {
  const params = new URLSearchParams();
  if (q) params.set('q', q);
  if (tag) params.set('tag', tag);
  const qs = params.toString() ? `?${params.toString()}` : '';
  return api.get<MemoryFactsResponse>(
    `/api/memory/${encodeURIComponent(agent)}${qs}`
  );
};

export const saveMemoryFact = (
  agent: string,
  payload: { key: string; value: string; tags?: string[] }
) => api.post<{ ok: boolean }>(`/api/memory/${encodeURIComponent(agent)}`, payload);

export const updateMemoryFact = (
  agent: string,
  key: string,
  payload: { value?: string; tags?: string[] }
) =>
  api.put<{ ok: boolean; item: MemoryFact }>(
    `/api/memory/${encodeURIComponent(agent)}/${encodeURIComponent(key)}`,
    payload
  );

export const deleteMemoryFact = (agent: string, key: string) =>
  api.del<{ ok: boolean }>(
    `/api/memory/${encodeURIComponent(agent)}/${encodeURIComponent(key)}`
  );

// ---------- Hire ----------

export interface HireDraftRequest {
  name: string;
  display_name: string;
  description: string;
  responsibilities: string;
  non_responsibilities: string;
  style: string;
  workflow_participant: boolean;
  needs_bash: boolean;
  needs_web: boolean;
  write_access: string[];
  read_access: string[];
}

export interface HireDraftResponse {
  yaml_entry: string;
  claude_md: string;
}

export interface HireApplyRequest {
  name: string;
  yaml_entry: string;
  claude_md: string;
}

export interface HireApplyResponse {
  name: string;
  note?: string;
  [k: string]: unknown;
}

export const hireDraft = (payload: HireDraftRequest) =>
  api.post<HireDraftResponse>('/api/hire/draft', payload);

export const hireApply = (payload: HireApplyRequest) =>
  api.post<HireApplyResponse>('/api/hire/apply', payload);

// ---------- Company / philosophy / agent policies ----------

export interface CompanyDocResponse {
  path: string;
  content: string;
}

export const getCompanyContext = () =>
  api.get<CompanyDocResponse>('/api/company/context');

export const setCompanyContext = (content: string) =>
  api.post<{ ok: boolean; size: number }>('/api/company/context', { content });

export const getCompanyPhilosophy = () =>
  api.get<CompanyDocResponse>('/api/company/philosophy');

export const setCompanyPhilosophy = (content: string) =>
  api.post<{ ok: boolean; size: number }>('/api/company/philosophy', { content });

export interface AgentMeta {
  name: string;
  display_name: string;
  username: string;
}

export const listAgentsMeta = () =>
  api.get<{ items: AgentMeta[] }>('/api/agents');

export interface AgentPolicy {
  agent: string;
  can_ask: string[] | null;
  can_be_asked_by: string[] | null;
  updated_at: string;
}

export const listAgentPolicies = () =>
  api.get<{ items: AgentPolicy[] }>('/api/agent-policies');

export const upsertAgentPolicy = (
  agent: string,
  can_ask: string[] | null,
  can_be_asked_by: string[] | null
) => api.post<{ ok: boolean }>('/api/agent-policies', { agent, can_ask, can_be_asked_by });

export const deleteAgentPolicy = (agent: string) =>
  api.del<{ ok: boolean }>(`/api/agent-policies/${encodeURIComponent(agent)}`);


// ---------- System prompts (D-63) ----------

export type SystemPromptToggleKey =
  | 'include_platform_prompt'
  | 'include_company_context'
  | 'include_company_philosophy'
  | 'include_agent_claude_md'
  | 'include_team_block'
  | 'include_invocation_context'
  | 'include_task_state'
  | 'include_step_instructions';

export type SystemPromptToggles = Record<SystemPromptToggleKey, boolean>;

export interface SystemPromptSection {
  key: string;
  title: string;
  source_path: string | null;
  present: boolean;
  size: number;
  toggle_key: SystemPromptToggleKey;
  enabled: boolean;
  generated: boolean;
  read_only?: boolean;
}

export interface SystemPromptAgentSection extends SystemPromptSection {
  name: string;
  display_name: string;
}

export interface SystemPromptIndex {
  toggles: SystemPromptToggles;
  sections: SystemPromptSection[];
  agents: SystemPromptAgentSection[];
}

export const getSystemPromptsIndex = () =>
  api.get<SystemPromptIndex>('/api/system-prompts');

export const getSystemPromptConfig = () =>
  api.get<{ toggles: SystemPromptToggles }>('/api/system-prompts/config');

export const setSystemPromptConfig = (toggles: Partial<SystemPromptToggles>) =>
  api.put<{ ok: boolean; toggles: SystemPromptToggles }>(
    '/api/system-prompts/config',
    { toggles }
  );

export interface SystemPromptSectionContent {
  key: string;
  source_path: string;
  content: string;
  present: boolean;
}

export const getSystemPromptSection = (key: string) =>
  api.get<SystemPromptSectionContent>(
    `/api/system-prompts/sections/${encodeURIComponent(key)}`
  );

export const setSystemPromptSection = (key: string, content: string) =>
  api.put<{ ok: boolean; size: number }>(
    `/api/system-prompts/sections/${encodeURIComponent(key)}`,
    { content }
  );

export interface SystemPromptPreview {
  agent: string | null;
  toggles: SystemPromptToggles;
  content: string;
}

export interface PreviewSimulationParams {
  agent?: string;
  mode?: 'root' | 'child';
  parent?: string;
  task_slug?: string;
}

export const getSystemPromptPreview = (params: PreviewSimulationParams = {}) => {
  const qp = new URLSearchParams();
  if (params.agent) qp.set('agent', params.agent);
  if (params.mode) qp.set('mode', params.mode);
  if (params.parent) qp.set('parent', params.parent);
  if (params.task_slug) qp.set('task_slug', params.task_slug);
  const qs = qp.toString() ? `?${qp.toString()}` : '';
  return api.get<SystemPromptPreview>(`/api/system-prompts/preview${qs}`);
};


// ---------- Onboarding ----------

export interface OnboardStatus {
  fresh: boolean;
  agents_count: number;
  has_context: boolean;
}

export const onboardStatus = () =>
  api.get<OnboardStatus>('/api/onboard/status');

export interface PhilosophyTemplate {
  slug: string;
  title: string;
  summary: string;
  content: string;
}

export const listPhilosophyTemplates = () =>
  api.get<{ items: PhilosophyTemplate[] }>('/api/philosophies/templates');

export interface ProposedAgent {
  slug: string;
  display_name: string;
  role: string;
  why: string;
  draft_claude_md: string;
}

export const onboardProposeAgents = (company_md: string, philosophy_md: string) =>
  api.post<{ agents: ProposedAgent[] }>('/api/onboard/propose-agents', {
    company_md,
    philosophy_md
  });

export const onboardApply = (
  company_md: string,
  philosophy_md: string,
  agents: Array<Pick<ProposedAgent, 'slug' | 'display_name' | 'role' | 'draft_claude_md'>>
) =>
  api.post<{ ok: boolean; created: string[]; errors: { slug: string; error: string }[] }>(
    '/api/onboard/apply',
    { company_md, philosophy_md, agents }
  );


// ---------- Pending asks ----------

export interface PendingAsk {
  id: string;            // "stream/topic" — backend rewrites in main.py
  conversation_id: number;
  stream: string;
  topic: string;
  asker_id: number;
  asker_username: string;
  question: string;
  context: string | null;
  blocking: boolean;
  asked_at: string;
}

export interface PendingAsksResponse {
  items: PendingAsk[];
}

export const listPendingAsks = () =>
  api.get<PendingAsksResponse>('/api/pending-asks');

// ---------- Live trace ----------

export type LiveEventKind =
  | 'run_start'
  | 'thinking'
  | 'tool_use'
  | 'tool_result'
  | 'run_end'
  | string;

export interface LiveEvent {
  id: number;
  /** Monotonic per conversation, generated in claude_runner (migration 018).
   *  Used as the primary tie-break in the feed when `ts` collides; falls back
   *  to `id` if absent (pre-migration events or synthetic events from mcp.server). */
  seq_num?: number;
  agent: string;
  ts: number;
  kind: LiveEventKind;
  summary: string;
  data?: Record<string, unknown>;
}

export const liveRecent = (convId: string, limit = 50) =>
  api.get<{ items: LiveEvent[] }>(
    `/api/conversations/${encodeConvId(convId)}/live/recent?limit=${limit}`
  );

export const liveStreamUrl = (convId: string): string =>
  `/api/conversations/${encodeConvId(convId)}/live/stream`;

export interface LiveEventFull {
  id: number;
  agent: string;
  kind: LiveEventKind;
  summary: string;
  ts: number | null;
  data: Record<string, unknown>;
}

export const liveEventFull = (id: number) =>
  api.get<LiveEventFull>(`/api/live_events/${id}/full`);

// ---------- Tasks (unified view) ----------

export interface TaskSummary {
  slug: string;
  title: string;
  status: string | null;
  current_step: string | null;
  current_agent: string | null;
  workflow: string | null;
  updated_at: string | null;
  phases_count: number;
  archived: boolean;
}

export interface TaskPhase {
  step?: string;
  agent?: string;
  started_at?: string;
  completed_at?: string;
  artifact?: string;
  summary?: string;
}

export interface TaskWorktree {
  path: string;
  branch: string;
  baseline_sha: string;
  registered_at?: string;
  registered_by?: string;
}

export interface TaskTimelineMsg {
  kind: 'msg';
  id: string;
  ts: number;
  conversation_id: number;
  stream: string;
  topic: string;
  sender: string;
  is_bot: boolean;
  content: string;
}

export interface TaskTimelineEvent {
  kind: 'event';
  id: string;
  ts: number;
  conversation_id: number;
  agent: string;
  event_kind: string;
  summary: string;
  data: Record<string, unknown>;
}

export type TaskTimelineItem = TaskTimelineMsg | TaskTimelineEvent;

export interface TaskTimelineResponse {
  slug: string;
  meta: {
    title?: string;
    status?: string | null;
    current_step?: string | null;
    current_agent?: string | null;
    workflow?: string | null;
    phases: TaskPhase[];
    worktrees: Record<string, TaskWorktree>;
  };
  conversations: { id: number; stream: string; topic: string }[];
  items: TaskTimelineItem[];
}

export const listTasks = (includeArchived = false) =>
  api.get<{ items: TaskSummary[] }>(
    `/api/tasks${includeArchived ? '?include_archived=1' : ''}`
  );

export const getTaskTimeline = (slug: string) =>
  api.get<TaskTimelineResponse>(`/api/tasks/${encodeURIComponent(slug)}/timeline`);

export const archiveTask = (slug: string) =>
  api.post<{ ok: boolean; slug: string; archived: boolean }>(
    `/api/tasks/${encodeURIComponent(slug)}/archive`
  );

export const unarchiveTask = (slug: string) =>
  api.post<{ ok: boolean; slug: string; archived: boolean }>(
    `/api/tasks/${encodeURIComponent(slug)}/unarchive`
  );

export const deleteTask = (slug: string) =>
  api.del<{ ok: boolean; slug: string; deleted: boolean }>(
    `/api/tasks/${encodeURIComponent(slug)}`
  );


// ---------- Backlog (D-53) ----------

export interface BacklogItem {
  slug: string;
  title: string;
  content: string | null;
  priority: number;
  impact: string | null;
  effort: string | null;
  status: string;                   // aberto | rascunho | em_execucao | promovido | descartado
  promoted_task_slug: string | null;
  created_by: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface BacklogListResponse {
  items: BacklogItem[];
}

export const listBacklog = (status?: string, includeAll = false) => {
  const qs: string[] = [];
  if (includeAll) qs.push('include_all=1');
  else if (status) qs.push(`status=${encodeURIComponent(status)}`);
  const q = qs.length ? `?${qs.join('&')}` : '';
  return api.get<BacklogListResponse>(`/api/backlog${q}`);
};

export interface BacklogCreateInput {
  slug: string;
  title: string;
  content?: string;
  priority?: number;
  impact?: string;
  effort?: string;
}

export const createBacklog = (item: BacklogCreateInput) =>
  api.post<{ ok: boolean; slug: string }>('/api/backlog', item);

export const patchBacklog = (slug: string, patch: Partial<BacklogCreateInput> & { status?: string }) =>
  api.patch<{ ok: boolean; slug: string; priority: number; status: string }>(
    `/api/backlog/${encodeURIComponent(slug)}`,
    patch,
  );

export const deleteBacklog = (slug: string) =>
  api.del<{ ok: boolean; slug: string; deleted: boolean }>(
    `/api/backlog/${encodeURIComponent(slug)}`,
  );

export interface BacklogPromoteInput {
  task_slug?: string;
  workflow?: string;
  next_agent: string;
  initial_topic?: string;
}

export const promoteBacklog = (slug: string, payload: BacklogPromoteInput) =>
  api.post<{ ok: boolean; backlog_slug: string; task_slug: string; next_agent: string }>(
    `/api/backlog/${encodeURIComponent(slug)}/promote`,
    payload,
  );

export const revertBacklog = (slug: string) =>
  api.post<{ ok: boolean; backlog_slug: string; task_slug: string }>(
    `/api/backlog/${encodeURIComponent(slug)}/revert`,
  );

export const reopenBacklog = (slug: string) =>
  api.post<{ ok: boolean; slug: string; status: string }>(
    `/api/backlog/${encodeURIComponent(slug)}/reopen`,
  );

export const forceResetBacklog = (slug: string) =>
  api.post<{
    ok: boolean;
    backlog_slug: string;
    task_slug: string | null;
    fs_removed?: boolean;
    no_task?: boolean;
  }>(`/api/backlog/${encodeURIComponent(slug)}/force-reset`);


// ---------- Search ----------

export interface SearchResult {
  message_id: number;
  stream: string;
  topic: string;
  conv_id: string;            // "stream/topic"
  sender: string;
  snippet: string;            // may contain <mark>highlight</mark>
  ts: number;
}

export const searchMessages = (q: string, limit = 30) =>
  api.get<{ items: SearchResult[] }>(
    `/api/search?q=${encodeURIComponent(q)}&limit=${limit}`
  );


// ---------- Auth ----------

export interface AuthMeResponse {
  user_id: number | null;
  username: string;
  kind: string;
  is_admin: boolean;
}

export const authMe = () => api.get<AuthMeResponse>('/api/auth/me');

export const authLogin = (email: string, password: string) =>
  api.post<{ ok: boolean; username: string; expires_at: string }>(
    '/api/auth/login',
    { email, password }
  );

export const authLogout = () => api.post<{ ok: boolean }>('/api/auth/logout');

export const setAdminPassword = (password: string) =>
  api.post<{ ok: boolean }>('/api/auth/set-password', { password });
