import { useState } from "react";
import { COMMON_TIMEZONES, CRON_PRESETS } from "../format";
import { inputClass } from "./ui";

// A friendlier layer on top of the raw cron field (ADR 0065): most schedules are "daily at a
// time", "weekly on a day", "every N hours/minutes" or a true sub-minute interval. Each of those
// maps onto a plain cron expression (or, for sub-minute, the separate IntervalTrigger type) so the
// wire format never grows a "frequency" concept of its own — this is purely a UI convenience.
// Anything that doesn't match one of those shapes (an arbitrary cron a user pasted in, or wrote by
// hand) falls back to "Custom", which edits the raw expression directly — nothing is ever lost.

export type TriggerShape = { type: "cron"; cron: string; timezone: string } | { type: "interval"; seconds: number };

/** Mirrors booth_pipeline.model.MIN_INTERVAL_SECONDS / MAX_INTERVAL_SECONDS. */
const MIN_INTERVAL_SECONDS = 5;
const MAX_INTERVAL_SECONDS = 30 * 24 * 3600;

type Freq = "daily" | "weekly" | "hourly" | "minutes" | "seconds" | "custom";

const WEEKDAYS = [
  { value: 0, label: "Sunday" },
  { value: 1, label: "Monday" },
  { value: 2, label: "Tuesday" },
  { value: 3, label: "Wednesday" },
  { value: 4, label: "Thursday" },
  { value: 5, label: "Friday" },
  { value: 6, label: "Saturday" },
];

type ParsedCron =
  | { freq: "daily"; hour: number; minute: number }
  | { freq: "weekly"; day: number; hour: number; minute: number }
  | { freq: "hourly"; everyN: number; minute: number }
  | { freq: "minutes"; everyN: number }
  | { freq: "custom" };

function parseCronFriendly(cron: string): ParsedCron {
  const parts = cron.trim().split(/\s+/);
  if (parts.length !== 5) return { freq: "custom" };
  const [min, hour, dom, month, dow] = parts;
  if (dom !== "*" || month !== "*") return { freq: "custom" };
  const minNum = /^\d+$/.test(min) ? Number(min) : null;
  const hourNum = /^\d+$/.test(hour) ? Number(hour) : null;
  if (minNum !== null && hourNum !== null && dow === "*") return { freq: "daily", hour: hourNum, minute: minNum };
  if (minNum !== null && hourNum !== null && /^[0-6]$/.test(dow)) return { freq: "weekly", day: Number(dow), hour: hourNum, minute: minNum };
  if (dow === "*") {
    const hourEvery = /^\*\/(\d+)$/.exec(hour);
    if (hourEvery && minNum !== null) return { freq: "hourly", everyN: Number(hourEvery[1]), minute: minNum };
    const minEvery = /^\*\/(\d+)$/.exec(min);
    if (minEvery && hour === "*") return { freq: "minutes", everyN: Number(minEvery[1]) };
  }
  return { freq: "custom" };
}

function pad(n: number): string {
  return String(n).padStart(2, "0");
}

interface FriendlyState {
  freq: Freq;
  hour: number;
  minute: number;
  day: number;
  hourlyN: number;
  minutesN: number;
  secondsN: number;
  cronRaw: string;
  timezone: string;
}

function toFriendly(value: TriggerShape, fallbackTimezone: string): FriendlyState {
  const base: FriendlyState = {
    freq: "daily",
    hour: 9,
    minute: 0,
    day: 1,
    hourlyN: 2,
    minutesN: 15,
    secondsN: MIN_INTERVAL_SECONDS,
    cronRaw: "0 9 * * *",
    timezone: fallbackTimezone,
  };
  if (value.type === "interval") return { ...base, freq: "seconds", secondsN: value.seconds };
  const parsed = parseCronFriendly(value.cron);
  const withCron = { ...base, cronRaw: value.cron, timezone: value.timezone };
  if (parsed.freq === "daily") return { ...withCron, freq: "daily", hour: parsed.hour, minute: parsed.minute };
  if (parsed.freq === "weekly") return { ...withCron, freq: "weekly", day: parsed.day, hour: parsed.hour, minute: parsed.minute };
  if (parsed.freq === "hourly") return { ...withCron, freq: "hourly", hourlyN: parsed.everyN, minute: parsed.minute };
  if (parsed.freq === "minutes") return { ...withCron, freq: "minutes", minutesN: parsed.everyN };
  return { ...withCron, freq: "custom" };
}

function toTrigger(f: FriendlyState): TriggerShape {
  if (f.freq === "seconds") return { type: "interval", seconds: f.secondsN };
  const cron =
    f.freq === "daily"
      ? `${f.minute} ${f.hour} * * *`
      : f.freq === "weekly"
        ? `${f.minute} ${f.hour} * * ${f.day}`
        : f.freq === "hourly"
          ? `${f.minute} */${f.hourlyN} * * *`
          : f.freq === "minutes"
            ? `*/${f.minutesN} * * * *`
            : f.cronRaw;
  return { type: "cron", cron, timezone: f.timezone };
}

export function ScheduleEditor({
  value,
  onChange,
  fallbackTimezone,
  fieldError,
}: {
  value: TriggerShape;
  onChange: (v: TriggerShape) => void;
  fallbackTimezone: string;
  fieldError?: (field: string) => string | undefined;
}) {
  // Initialized once from the incoming value, same one-shot-from-props pattern the rest of this
  // form uses (see JobForm) — the friendly fields are the source of truth for further edits, not
  // re-derived from `value` on every keystroke, so a user's in-progress "Custom" text isn't
  // fought over by the parser.
  const [f, setF] = useState<FriendlyState>(() => toFriendly(value, fallbackTimezone));

  function update(patch: Partial<FriendlyState>) {
    const next = { ...f, ...patch };
    setF(next);
    onChange(toTrigger(next));
  }

  const showTimezone = f.freq !== "seconds";
  const timeField = (
    <input
      type="time"
      className={inputClass}
      value={`${pad(f.hour)}:${pad(f.minute)}`}
      onChange={(e) => {
        const [h, m] = e.target.value.split(":").map(Number);
        update({ hour: h || 0, minute: m || 0 });
      }}
    />
  );

  return (
    <div className="flex flex-col gap-3">
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="flex flex-col gap-1">
          <span className="text-xs font-medium text-slate-600 dark:text-slate-300">Frequency</span>
          <select className={inputClass} value={f.freq} onChange={(e) => update({ freq: e.target.value as Freq })}>
            <option value="daily">Daily, at a time</option>
            <option value="weekly">Weekly, on a day</option>
            <option value="hourly">Every N hours</option>
            <option value="minutes">Every N minutes</option>
            <option value="seconds">Every N seconds</option>
            <option value="custom">Custom (cron expression)</option>
          </select>
        </label>

        {f.freq === "daily" && (
          <label className="flex flex-col gap-1">
            <span className="text-xs font-medium text-slate-600 dark:text-slate-300">Time</span>
            {timeField}
          </label>
        )}

        {f.freq === "weekly" && (
          <label className="flex flex-col gap-1">
            <span className="text-xs font-medium text-slate-600 dark:text-slate-300">Day</span>
            <select className={inputClass} value={f.day} onChange={(e) => update({ day: Number(e.target.value) })}>
              {WEEKDAYS.map((d) => (
                <option key={d.value} value={d.value}>
                  {d.label}
                </option>
              ))}
            </select>
          </label>
        )}

        {f.freq === "weekly" && (
          <label className="flex flex-col gap-1">
            <span className="text-xs font-medium text-slate-600 dark:text-slate-300">Time</span>
            {timeField}
          </label>
        )}

        {f.freq === "hourly" && (
          <label className="flex flex-col gap-1">
            <span className="text-xs font-medium text-slate-600 dark:text-slate-300">Every how many hours</span>
            <input
              type="number"
              min={1}
              max={23}
              className={inputClass}
              value={f.hourlyN}
              onChange={(e) => update({ hourlyN: Math.min(23, Math.max(1, Math.trunc(Number(e.target.value)) || 1)) })}
            />
          </label>
        )}

        {f.freq === "minutes" && (
          <label className="flex flex-col gap-1">
            <span className="text-xs font-medium text-slate-600 dark:text-slate-300">Every how many minutes</span>
            <input
              type="number"
              min={1}
              max={59}
              className={inputClass}
              value={f.minutesN}
              onChange={(e) => update({ minutesN: Math.min(59, Math.max(1, Math.trunc(Number(e.target.value)) || 1)) })}
            />
          </label>
        )}

        {f.freq === "seconds" && (
          <label className="flex flex-col gap-1">
            <span className="text-xs font-medium text-slate-600 dark:text-slate-300">Every how many seconds</span>
            <input
              type="number"
              min={MIN_INTERVAL_SECONDS}
              max={MAX_INTERVAL_SECONDS}
              className={inputClass}
              value={f.secondsN}
              onChange={(e) => update({ secondsN: Math.min(MAX_INTERVAL_SECONDS, Math.max(MIN_INTERVAL_SECONDS, Math.trunc(Number(e.target.value)) || MIN_INTERVAL_SECONDS)) })}
            />
          </label>
        )}
      </div>

      {f.freq === "custom" && (
        <div className="flex flex-col gap-1">
          <label className="flex flex-col gap-1">
            <span className="text-xs font-medium text-slate-600 dark:text-slate-300">Cron expression</span>
            <input
              className={`${inputClass} font-mono`}
              value={f.cronRaw}
              list="cron-presets"
              onChange={(e) => update({ cronRaw: e.target.value })}
            />
          </label>
          <datalist id="cron-presets">
            {CRON_PRESETS.map((c) => (
              <option key={c.cron} value={c.cron}>
                {c.label}
              </option>
            ))}
          </datalist>
          {fieldError?.("schedule.cron") && <p className="text-xs text-red-600 dark:text-red-400">{fieldError("schedule.cron")}</p>}
          <p className="text-xs text-slate-500 dark:text-slate-400">Five fields: minute hour day-of-month month day-of-week.</p>
        </div>
      )}

      {f.freq === "seconds" && fieldError?.("schedule.interval.seconds") && (
        <p className="text-xs text-red-600 dark:text-red-400">{fieldError("schedule.interval.seconds")}</p>
      )}
      {f.freq !== "seconds" && f.freq !== "custom" && fieldError?.("schedule.cron") && (
        <p className="text-xs text-red-600 dark:text-red-400">{fieldError("schedule.cron")}</p>
      )}

      {showTimezone && (
        <div className="flex max-w-xs flex-col gap-1">
          <label className="flex flex-col gap-1">
            <span className="text-xs font-medium text-slate-600 dark:text-slate-300">Timezone</span>
            <input className={inputClass} value={f.timezone} list="timezones" onChange={(e) => update({ timezone: e.target.value })} />
          </label>
          <datalist id="timezones">
            {COMMON_TIMEZONES.map((t) => (
              <option key={t} value={t} />
            ))}
          </datalist>
          <p className="text-xs text-slate-500 dark:text-slate-400">Evaluated in this timezone, so a time of day stays put across daylight saving.</p>
        </div>
      )}
    </div>
  );
}
