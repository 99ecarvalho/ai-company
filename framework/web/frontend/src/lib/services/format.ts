/**
 * Display formatters — ports of fmtAge/fmtTime/fmtCost/fmtMs/fmtNum from
 * the legacy app.js. Same outputs.
 */

export function fmtAge(tsSeconds: number, nowMs: number = Date.now()): string {
  const s = Math.max(0, Math.floor(nowMs / 1000) - tsSeconds);
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}

export function fmtTime(ms: number): string {
  const s = Math.floor(ms / 1000);
  return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
}

export function fmtClock(tsSeconds: number): string {
  return new Date(tsSeconds * 1000).toLocaleTimeString();
}

/** True when both timestamps fall on the same calendar day in local time. */
export function sameDay(tsA: number, tsB: number): boolean {
  const a = new Date(tsA * 1000);
  const b = new Date(tsB * 1000);
  return (
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate()
  );
}

const DATE_SEPARATOR_FMT = new Intl.DateTimeFormat(undefined, {
  day: 'numeric',
  month: 'short',
  year: 'numeric'
});

/** WhatsApp-style day divider: "Today" / "Yesterday" / "Apr 27, 2026". */
export function fmtDateSeparator(tsSeconds: number, nowMs: number = Date.now()): string {
  const target = new Date(tsSeconds * 1000);
  const now = new Date(nowMs);
  const dayMs = 86400 * 1000;
  const startOfDay = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const diffDays = Math.round((startOfDay(now) - startOfDay(target)) / dayMs);
  if (diffDays === 0) return 'Today';
  if (diffDays === 1) return 'Yesterday';
  return DATE_SEPARATOR_FMT.format(target);
}

export function fmtCost(v: number | null | undefined): string {
  return `$${(v || 0).toFixed(4)}`;
}

export function fmtMs(v: number | null | undefined): string {
  if (!v) return '—';
  if (v < 1000) return `${Math.round(v)}ms`;
  return `${(v / 1000).toFixed(1)}s`;
}

export function fmtNum(v: number | null | undefined): string {
  if (!v) return '0';
  if (v >= 1e6) return `${(v / 1e6).toFixed(1)}M`;
  if (v >= 1e3) return `${(v / 1e3).toFixed(1)}k`;
  return String(v);
}

export function fmtBytes(b: number | null | undefined): string {
  if (b == null) return '';
  if (b >= 1024) return `${Math.round(b / 1024)}KB`;
  return `${b}B`;
}
