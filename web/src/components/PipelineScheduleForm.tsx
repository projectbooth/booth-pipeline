import { useState, type FormEvent } from "react";
import { api } from "../api/client";
import type { ViewCtx } from "../context";
import { formatTime, localTimezone } from "../format";
import { errorMessage } from "../hooks";
import type { Pipeline, PipelineScheduleUpdate, Trigger } from "../types";
import { ScheduleEditor, type TriggerShape } from "./ScheduleEditor";
import { Banner, Button, Field, inputClass } from "./ui";

// A pipeline's own schedule, unattended-run settings and version pin (ADR 0071: these lived on the
// retired Job entity; Pipeline owns them directly now, since it is the only thing that ever runs
// unattended). No default retry policy here any more — a task's own retry lives purely on its own
// TaskConfig, with no pipeline-level fallback (ADR 0071).

export function PipelineScheduleForm({
  v,
  pipeline,
  versions,
  onSaved,
}: {
  v: ViewCtx;
  pipeline: Pipeline;
  versions: { version: number; createdAt: string }[];
  onSaved: (p: Pipeline) => void;
}) {
  const readOnly = !v.canWrite;
  const [scheduled, setScheduled] = useState(pipeline.schedule != null);
  const [shape, setShape] = useState<TriggerShape>(() =>
    pipeline.schedule?.type === "interval"
      ? { type: "interval", seconds: pipeline.schedule.seconds }
      : { type: "cron", cron: pipeline.schedule?.cron ?? "0 9 * * *", timezone: pipeline.schedule?.timezone ?? localTimezone() },
  );
  const [enabled, setEnabled] = useState(pipeline.schedule?.enabled ?? true);
  const [concurrent, setConcurrent] = useState(pipeline.allowConcurrentRuns);
  const [ceiling, setCeiling] = useState<"viewer" | "editor">(pipeline.roleCeiling);
  const [pinned, setPinned] = useState<number | null>(pipeline.pinnedVersion);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ message: string; field?: string } | null>(null);
  const [saved, setSaved] = useState(false);

  async function submit(ev: FormEvent) {
    ev.preventDefault();
    setBusy(true);
    setError(null);
    setSaved(false);
    const schedule: Trigger | null = scheduled
      ? shape.type === "interval"
        ? { type: "interval", seconds: shape.seconds, enabled }
        : { type: "cron", cron: shape.cron.trim(), timezone: shape.timezone.trim(), enabled }
      : null;
    const body: PipelineScheduleUpdate = { schedule, allowConcurrentRuns: concurrent, roleCeiling: ceiling, pinnedVersion: pinned };
    try {
      const p = await api.updateSchedule(v.api, pipeline.id, body);
      setSaved(true);
      onSaved(p);
    } catch (err) {
      setError({ message: errorMessage(err), field: (err as { field?: string }).field });
    } finally {
      setBusy(false);
    }
  }

  const fieldError = (f: string) => (error?.field === f ? error.message : undefined);

  return (
    <form onSubmit={submit} aria-label="Pipeline schedule" className="flex flex-col gap-4">
      <fieldset disabled={readOnly || busy} className="flex flex-col gap-4">
        <Field id="pipeline-version-pin" label="Version" error={fieldError("pinnedVersion")} help="Follow the latest to pick up every save, or pin one so runs never change out from under you.">
          {(p) => (
            <select {...p} className={inputClass} value={pinned ?? ""} onChange={(e) => setPinned(e.target.value === "" ? null : Number(e.target.value))}>
              <option value="">Always the latest saved version</option>
              {versions.map((ver) => (
                <option key={ver.version} value={ver.version}>
                  Pin to v{ver.version} — {formatTime(ver.createdAt)}
                </option>
              ))}
            </select>
          )}
        </Field>

        <section aria-label="Schedule" className="flex flex-col gap-2 rounded-md border border-slate-200 p-3 dark:border-slate-700">
          <label className="flex items-center gap-2 text-sm font-medium text-slate-800 dark:text-slate-100">
            <input type="checkbox" checked={scheduled} onChange={(e) => setScheduled(e.target.checked)} />
            Run on a schedule
          </label>
          {scheduled ? (
            <>
              <ScheduleEditor value={shape} onChange={setShape} fallbackTimezone={localTimezone()} fieldError={fieldError} />
              <label className="flex items-center gap-2 text-sm text-slate-700 dark:text-slate-300">
                <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
                Schedule is active (untick to pause without losing it)
              </label>
            </>
          ) : (
            <p className="text-xs text-slate-500 dark:text-slate-400">Without a schedule this pipeline only runs when you press "Run now".</p>
          )}
        </section>

        <Field
          id="pipeline-ceiling"
          label="Role ceiling for platform access"
          help="Tasks that opt in to platform access get a token for this run capped at this role — and never above what the pipeline's owner (whoever first saved a schedule onto it) currently holds. There is no owner ceiling: nothing a pipeline does needs workspace administration."
        >
          {(p) => (
            <select {...p} className={inputClass} value={ceiling} onChange={(e) => setCeiling(e.target.value as "viewer" | "editor")}>
              <option value="editor">Editor — can read and write storage and the catalog</option>
              <option value="viewer">Viewer — read only</option>
            </select>
          )}
        </Field>
        {!pipeline.hasOwner && (pipeline.schedule || scheduled) && (
          <Banner tone="warn">
            This pipeline predates workload identity and has no owner, so its scheduled runs cannot get platform access. Saving a schedule here makes you its owner.
          </Banner>
        )}

        <label className="flex items-center gap-2 text-sm text-slate-700 dark:text-slate-300">
          <input type="checkbox" checked={concurrent} onChange={(e) => setConcurrent(e.target.checked)} />
          Allow overlapping runs (otherwise a new run is refused while one is still active)
        </label>
      </fieldset>

      {error && !error.field && <Banner tone="error">{error.message}</Banner>}
      {error?.field && !["schedule.cron", "schedule.interval.seconds", "pinnedVersion"].includes(error.field) && <Banner tone="error">{error.message}</Banner>}
      {saved && <Banner tone="success">Schedule saved.</Banner>}
      {!readOnly && (
        <div>
          <Button type="submit" variant="primary" disabled={busy}>
            {busy ? "Saving…" : "Save schedule"}
          </Button>
        </div>
      )}
    </form>
  );
}
