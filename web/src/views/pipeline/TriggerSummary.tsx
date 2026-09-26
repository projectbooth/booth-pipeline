import { describeInterval, describeSchedule } from "../../format";
import type { Trigger } from "../../types";
import { Badge } from "../../ui/primitives";

/** A pipeline's trigger at a glance: "Manual only", a cron expression with its plain-English
 *  reading, or an interval — and "Paused" when a schedule exists but is disabled. */
export function TriggerSummary({ schedule, compact }: { schedule: Trigger | null; compact?: boolean }) {
  if (!schedule) return <span className="text-sm text-slate-500 dark:text-slate-400">Manual only</span>;
  const label = schedule.type === "cron" ? schedule.cron : describeInterval(schedule.seconds);
  return (
    <span className="inline-flex flex-col gap-0.5" title={describeSchedule(schedule)}>
      <span className="inline-flex items-center gap-1.5">
        <Badge tone={schedule.enabled ? "indigo" : "slate"} mono={schedule.type === "cron"}>
          {schedule.type === "cron" ? "⏱" : "↻"} {label}
        </Badge>
        {!schedule.enabled && <Badge tone="amber">Paused</Badge>}
      </span>
      {!compact && schedule.type === "cron" && <span className="text-xs text-slate-500 dark:text-slate-400">{describeSchedule(schedule)}</span>}
    </span>
  );
}
