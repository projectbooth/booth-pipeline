import type { Schedule } from "./types";

export function formatTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

/** "1m 05s" / "12s" / "340ms"; empty when the interval is not known. Takes the end as a parameter
 *  (rather than reading the clock) so a running task's duration ticks with the caller's own timer. */
export function formatDuration(startIso: string | null | undefined, endIso: string | null | undefined, now: number = Date.now()): string {
  if (!startIso) return "—";
  const start = new Date(startIso).getTime();
  const end = endIso ? new Date(endIso).getTime() : now;
  if (Number.isNaN(start) || Number.isNaN(end)) return "—";
  const ms = Math.max(0, end - start);
  if (ms < 1000) return `${ms}ms`;
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, "0")}s`;
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
}

const PRESETS: Record<string, string> = {
  "* * * * *": "every minute",
  "*/5 * * * *": "every 5 minutes",
  "*/15 * * * *": "every 15 minutes",
  "0 * * * *": "hourly",
  "0 0 * * *": "daily at midnight",
  "0 9 * * *": "daily at 09:00",
  "0 9 * * 1-5": "weekdays at 09:00",
  "0 0 * * 0": "weekly, Sunday at midnight",
};

export const CRON_PRESETS: { label: string; cron: string }[] = Object.entries(PRESETS).map(([cron, label]) => ({ cron, label }));

export function describeSchedule(s: Schedule | null): string {
  if (!s) return "On demand only";
  const text = PRESETS[s.cron] ?? `cron ${s.cron}`;
  return `${text} (${s.timezone})${s.enabled ? "" : " — paused"}`;
}

/** The browser's IANA timezone, as a sensible default for a new schedule. */
export function localTimezone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

export const COMMON_TIMEZONES = [
  "UTC",
  "America/Toronto",
  "America/New_York",
  "America/Chicago",
  "America/Denver",
  "America/Los_Angeles",
  "America/Sao_Paulo",
  "Europe/London",
  "Europe/Paris",
  "Europe/Berlin",
  "Africa/Johannesburg",
  "Asia/Dubai",
  "Asia/Kolkata",
  "Asia/Singapore",
  "Asia/Tokyo",
  "Australia/Sydney",
];
