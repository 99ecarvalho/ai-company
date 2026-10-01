import { writable, get, derived } from 'svelte/store';
import { listTasks, type TaskSummary } from '$lib/api';
import { createSSEClient, coalesceRefresh, type SSEClient } from '$lib/sse';
import { logEvent } from './ui';

export type TaskFilter = 'active' | 'done' | 'archived';

export const tasks = writable<TaskSummary[]>([]);
export const taskFilter = writable<TaskFilter>('active');

// Local query for the task list. Applied to slug/title/workflow/current_agent
// (case-insensitive). Zero API — FTS over artifacts is left for another round.
export const taskQuery = writable<string>('');

// Whether to include archived ones in the fetch: only when the filter is 'archived'.
// 'active' and 'done' use the lean default (no _archive/).
const includeArchivedForFilter = (f: TaskFilter) => f === 'archived';

export const filteredTasks = derived([tasks, taskFilter], ([$tasks, $filter]) => {
  return $tasks.filter((t) => {
    if ($filter === 'archived') return t.archived;
    if (t.archived) return false;
    if ($filter === 'done') return t.status === 'done';
    // 'active' — everything non-archived that isn't done. Includes null/in_progress/halt/human_review.
    return t.status !== 'done';
  });
});

/** Composition: filteredTasks + taskQuery. Consumed by the /tasks list page. */
export const visibleTasks = derived(
  [filteredTasks, taskQuery],
  ([items, q]) => {
    const needle = q.trim().toLowerCase();
    if (!needle) return items;
    return items.filter(
      (t) =>
        (t.slug || '').toLowerCase().includes(needle) ||
        (t.title || '').toLowerCase().includes(needle) ||
        (t.workflow || '').toLowerCase().includes(needle) ||
        (t.current_agent || '').toLowerCase().includes(needle)
    );
  }
);

let sseClient: SSEClient | null = null;
let inFlight = false;

export async function refreshTasks() {
  if (inFlight) return;
  inFlight = true;
  try {
    const { items } = await listTasks(includeArchivedForFilter(get(taskFilter)));
    tasks.set(items);
  } catch (e) {
    logEvent(`refresh tasks: ${e}`, 'err');
  } finally {
    inFlight = false;
  }
}

// When the filter changes, re-fetch (to include/exclude archived ones).
taskFilter.subscribe(() => {
  if (sseClient) refreshTasks();
});

const refreshTriggered = coalesceRefresh(refreshTasks);

export function startTasksStream() {
  if (sseClient) return;
  refreshTasks();
  sseClient = createSSEClient({
    url: '/api/tasks/events',
    onMessage: () => refreshTriggered(),
    onOpen: () => refreshTriggered()
  });
}

export function stopTasksStream() {
  if (sseClient) {
    sseClient.close();
    sseClient = null;
  }
}

export function findTask(slug: string): TaskSummary | undefined {
  return get(tasks).find((t) => t.slug === slug);
}
