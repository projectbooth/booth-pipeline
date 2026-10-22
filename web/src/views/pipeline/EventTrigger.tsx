import { useEffect, useState } from "react";
import { api } from "../../api/client";
import type { ViewCtx } from "../../context";
import { formatRelative, formatTime } from "../../format";
import { errorMessage, useInterval, useLoad } from "../../hooks";
import type { EventMatch, EventTrigger, EventTriggerStatus, StorageBackendSummary, TriggerEventRecord } from "../../types";
import { Banner, Button, Field, inputClass } from "../../ui/primitives";

// ADR 0123: the event trigger on the Schedule & Triggers tab — its summary with a live status line,
// and its editor. It sits beside the schedule: a pipeline can have one of each, and the slow schedule
// is the safety net for events the bus may lose.

export const DEFAULT_EVENT_TRIGGER: EventTrigger = {
  type: "event",
  enabled: true,
  match: [{ storage: { backendId: "", prefix: "" } }],
  debounceSeconds: 10,
  maxWaitSeconds: 60,
  whenRunning: "queue_one",
  minGapSeconds: 5,
};

/** What isn't covered, said where the trigger is set up (ADR 0123 point 5 and its revision). */
export function EventTriggerHelp() {
  return (
    <p className="text-xs text-slate-500 dark:text-slate-400">
      Fires on files created through booth-storage (the Storage page, an app upload, the API) and rows written through booth-api. It does not see files a task
      or Spark writes straight to object storage, or rows written straight to PostgreSQL. Storage events can be missed while the event bus is down: keep a slow
      schedule as well if a missed file matters. A pipeline that writes into a folder it watches through another identity can trigger itself.
    </p>
  );
}

export function describeMatch(m: EventMatch): string {
  if (m.storage) return `Files in ${m.storage.backendId}:${m.storage.prefix || "(the whole backend)"}`;
  if (m.api?.slugPrefix) return `Writes to APIs starting ${m.api.slugPrefix}`;
  return `Writes to ${(m.api?.slugs ?? []).join(", ")}`;
}

function seconds(n: number): string {
  return n % 60 === 0 && n >= 60 ? `${n / 60} min` : `${n} s`;
}

export function describeTiming(t: EventTrigger, concurrent: boolean): string[] {
  return [
    `Runs ${seconds(t.debounceSeconds)} after the last event, at most ${seconds(t.maxWaitSeconds)} after the first, and at least ${seconds(t.minGapSeconds)} after its last run.`,
    concurrent
      ? "Overlapping runs are allowed, so each burst starts its own run."
      : t.whenRunning === "queue_one"
        ? "If a run is active, it runs once more afterwards."
        : "If a run is active, events that arrive during it are ignored.",
  ];
}

function describeEvent(e: TriggerEventRecord | { type: string }): string {
  const r = e as TriggerEventRecord;
  if (r.path !== undefined) return `${r.backendId}:${r.path}${r.principal ? ` (${r.principal})` : ""}`;
  if (r.apiSlug !== undefined) return `${r.operation ?? "write"} to ${r.apiSlug}${r.principal ? ` (${r.principal})` : ""}`;
  return e.type === "api.write.committed" ? "a write through booth-api" : "a file created in booth-storage";
}

/** The live line under the trigger: last event, next run, and why it may not have fired. */
export function EventTriggerStatusLine({ v, pipelineId }: { v: ViewCtx; pipelineId: string }) {
  const load = useLoad(() => api.eventTriggerStatus(v.api, pipelineId), [v.api, pipelineId]);
  const s: EventTriggerStatus | null = load.state.status === "ready" ? load.state.data : null;
  useInterval(load.reload, 3000, Boolean(s && (s.nextFireAt || s.queued)));
  if (load.state.status === "error") return <p className="text-xs text-red-600 dark:text-red-400">Trigger status unavailable: {load.state.error}</p>;
  if (!s) return null;
  const lines: string[] = [];
  lines.push(s.lastEvent && s.lastEventAt ? `Last event ${formatTime(s.lastEventAt)} — ${describeEvent(s.lastEvent)}` : "No event seen yet.");
  if (s.nextFireAt) lines.push(`Next run ${formatRelative(s.nextFireAt)} (${s.pendingEvents} event${s.pendingEvents === 1 ? "" : "s"} waiting)`);
  else if (s.queued) lines.push(`Waiting for the active run, then runs once (${s.pendingEvents} event${s.pendingEvents === 1 ? "" : "s"} waiting)`);
  if (s.lastFiredAt) lines.push(`Last fired ${formatTime(s.lastFiredAt)}`);
  if (s.ignoredOwnRuns) lines.push(`Ignored ${s.ignoredOwnRuns} event${s.ignoredOwnRuns === 1 ? "" : "s"} written by this pipeline's own runs`);
  if (s.skippedWhileRunning) lines.push(`Skipped ${s.skippedWhileRunning} event${s.skippedWhileRunning === 1 ? "" : "s"} that arrived during a run`);
  return (
    <div aria-label="Event trigger status" className="flex flex-col gap-1 text-xs text-slate-600 dark:text-slate-300">
      {lines.map((l) => (
        <span key={l}>{l}</span>
      ))}
      {s.missedEventsAt && (
        <Banner tone="warn">
          Events may have been missed: no replica read the event bus for longer than it keeps events, so this trigger fired once at {formatTime(s.missedEventsAt)} to
          catch up.
        </Banner>
      )}
      {s.bus.configured && !s.bus.connected && s.enabled && <Banner tone="warn">This replica isn't connected to the event bus right now; events wait on the bus until it is.</Banner>}
    </div>
  );
}

interface Row {
  kind: "storage" | "api";
  backendId: string;
  prefix: string;
  apiMode: "slugs" | "prefix";
  slugs: string;
  slugPrefix: string;
}

function toRow(m: EventMatch): Row {
  if (m.api)
    return {
      kind: "api",
      backendId: "",
      prefix: "",
      apiMode: m.api.slugPrefix ? "prefix" : "slugs",
      slugs: (m.api.slugs ?? []).join(", "),
      slugPrefix: m.api.slugPrefix ?? "",
    };
  return { kind: "storage", backendId: m.storage?.backendId ?? "", prefix: m.storage?.prefix ?? "", apiMode: "slugs", slugs: "", slugPrefix: "" };
}

function toMatch(r: Row): EventMatch {
  if (r.kind === "storage") return { storage: { backendId: r.backendId, prefix: r.prefix.trim() } };
  if (r.apiMode === "prefix") return { api: { slugPrefix: r.slugPrefix.trim() } };
  return {
    api: {
      slugs: r.slugs
        .split(",")
        .map((x) => x.trim())
        .filter(Boolean),
    },
  };
}

/** The event trigger's editor: controlled, emitting a whole EventTrigger on every change. */
export function EventTriggerEditor({
  v,
  value,
  onChange,
  concurrent,
  fieldError,
}: {
  v: ViewCtx;
  value: EventTrigger;
  onChange: (t: EventTrigger) => void;
  concurrent: boolean;
  fieldError: (field: string) => string | undefined;
}) {
  const [rows, setRows] = useState<Row[]>(() => value.match.map(toRow));
  const [backends, setBackends] = useState<StorageBackendSummary[] | null>(null);
  const [backendError, setBackendError] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    api.storageBackends(v.api).then(
      (list) => alive && setBackends(list),
      (err: unknown) => alive && setBackendError(errorMessage(err)),
    );
    return () => {
      alive = false;
    };
  }, [v.api]);

  function setRowsAndEmit(next: Row[]) {
    setRows(next);
    onChange({ ...value, match: next.map(toMatch) });
  }
  const update = (i: number, patch: Partial<Row>) => setRowsAndEmit(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  const num = (k: "debounceSeconds" | "maxWaitSeconds" | "minGapSeconds") => (e: React.ChangeEvent<HTMLInputElement>) =>
    onChange({ ...value, [k]: e.target.value === "" ? 0 : Number(e.target.value) });

  return (
    <div className="flex flex-col gap-4">
      <EventTriggerHelp />
      {backendError && <Banner tone="warn">Storage backends could not be listed ({backendError}); type the backend id instead.</Banner>}
      <ol aria-label="What the trigger watches" className="flex flex-col gap-3">
        {rows.map((r, i) => (
          <li key={i} className="flex flex-col gap-2 rounded-md border border-slate-200 p-3 dark:border-slate-700">
            <div className="flex flex-wrap items-end gap-2">
              <Field id={`ev-kind-${i}`} label="Watch">
                {(fp) => (
                  <select {...fp} className={inputClass} value={r.kind} onChange={(e) => update(i, { kind: e.target.value as Row["kind"] })}>
                    <option value="storage">Files created in Storage</option>
                    <option value="api">Writes through an API</option>
                  </select>
                )}
              </Field>
              {rows.length > 1 && (
                <Button size="sm" onClick={() => setRowsAndEmit(rows.filter((_, j) => j !== i))} aria-label={`Remove watch ${i + 1}`}>
                  Remove
                </Button>
              )}
            </div>
            {r.kind === "storage" ? (
              <div className="grid gap-2 sm:grid-cols-2">
                <Field id={`ev-backend-${i}`} label="Backend" error={fieldError(`eventTrigger.match[${i}].storage.backendId`)}>
                  {(fp) =>
                    backends ? (
                      <select {...fp} className={inputClass} value={r.backendId} onChange={(e) => update(i, { backendId: e.target.value })}>
                        <option value="">Choose a backend…</option>
                        {backends.map((b) => (
                          <option key={b.id} value={b.id}>
                            {b.displayName || b.id}
                          </option>
                        ))}
                      </select>
                    ) : (
                      <input {...fp} className={inputClass} value={r.backendId} onChange={(e) => update(i, { backendId: e.target.value })} />
                    )
                  }
                </Field>
                <Field id={`ev-prefix-${i}`} label="Folder" help="Files anywhere under it, e.g. finance/dev/inbox/. Empty: the whole backend.">
                  {(fp) => <input {...fp} className={inputClass} value={r.prefix} placeholder="finance/dev/inbox/" onChange={(e) => update(i, { prefix: e.target.value })} />}
                </Field>
              </div>
            ) : (
              <div className="grid gap-2 sm:grid-cols-2">
                <Field id={`ev-apimode-${i}`} label="APIs">
                  {(fp) => (
                    <select {...fp} className={inputClass} value={r.apiMode} onChange={(e) => update(i, { apiMode: e.target.value as Row["apiMode"] })}>
                      <option value="slugs">These API slugs</option>
                      <option value="prefix">Every API whose slug starts with</option>
                    </select>
                  )}
                </Field>
                {r.apiMode === "slugs" ? (
                  <Field id={`ev-slugs-${i}`} label="Slugs" help="Comma-separated.">
                    {(fp) => <input {...fp} className={inputClass} value={r.slugs} placeholder="finance-dev-tx" onChange={(e) => update(i, { slugs: e.target.value })} />}
                  </Field>
                ) : (
                  <Field id={`ev-slugprefix-${i}`} label="Slug starts with" help="Also covers APIs created later.">
                    {(fp) => <input {...fp} className={inputClass} value={r.slugPrefix} placeholder="finance-dev-" onChange={(e) => update(i, { slugPrefix: e.target.value })} />}
                  </Field>
                )}
              </div>
            )}
          </li>
        ))}
      </ol>
      {rows.length < 10 && (
        <div>
          <Button size="sm" onClick={() => setRowsAndEmit([...rows, toRow({ storage: { backendId: "", prefix: "" } })])}>
            Watch something else
          </Button>
        </div>
      )}
      <div className="grid gap-3 sm:grid-cols-3">
        <Field id="ev-debounce" label="Wait for quiet (seconds)" help="Runs this long after the last event." error={fieldError("eventTrigger.debounceSeconds")}>
          {(fp) => <input {...fp} type="number" min={1} max={3600} className={inputClass} value={value.debounceSeconds} onChange={num("debounceSeconds")} />}
        </Field>
        <Field id="ev-maxwait" label="Wait at most (seconds)" help="During a long burst, runs this long after its first event." error={fieldError("eventTrigger.maxWaitSeconds")}>
          {(fp) => <input {...fp} type="number" min={1} max={86400} className={inputClass} value={value.maxWaitSeconds} onChange={num("maxWaitSeconds")} />}
        </Field>
        <Field id="ev-gap" label="Minimum gap (seconds)" help="Between two runs this trigger starts." error={fieldError("eventTrigger.minGapSeconds")}>
          {(fp) => <input {...fp} type="number" min={5} max={86400} className={inputClass} value={value.minGapSeconds} onChange={num("minGapSeconds")} />}
        </Field>
      </div>
      {concurrent ? (
        <p className="text-xs text-slate-500 dark:text-slate-400">Overlapping runs are allowed, so each burst starts its own run.</p>
      ) : (
        <fieldset className="flex flex-col gap-1 text-sm text-slate-700 dark:text-slate-300">
          <legend className="text-xs font-medium text-slate-600 dark:text-slate-300">If a run is already active</legend>
          <label className="flex items-center gap-2">
            <input type="radio" name="ev-when-running" checked={value.whenRunning === "queue_one"} onChange={() => onChange({ ...value, whenRunning: "queue_one" })} />
            Run once more after it ends
          </label>
          <label className="flex items-center gap-2">
            <input type="radio" name="ev-when-running" checked={value.whenRunning === "skip"} onChange={() => onChange({ ...value, whenRunning: "skip" })} />
            Ignore events that arrive during it
          </label>
        </fieldset>
      )}
      <label className="flex items-center gap-2 text-sm text-slate-700 dark:text-slate-300">
        <input type="checkbox" checked={value.enabled} onChange={(e) => onChange({ ...value, enabled: e.target.checked })} />
        Event trigger is active (untick to pause it without losing it)
      </label>
    </div>
  );
}
