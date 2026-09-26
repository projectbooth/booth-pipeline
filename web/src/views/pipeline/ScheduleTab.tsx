import { useState, type FormEvent } from "react";
import { api } from "../../api/client";
import type { ViewCtx } from "../../context";
import { describeInterval, describeSchedule, formatRelative, formatStamp, formatTime, localTimezone } from "../../format";
import { errorMessage } from "../../hooks";
import { ScheduleEditor, type TriggerShape } from "../../task/ScheduleEditor";
import type { PipelineScheduleUpdate, Trigger } from "../../types";
import { Banner, Button, Card, Field, KVRows, inputClass, type KV } from "../../ui/primitives";
import type { PipelineData } from "./data";

// S2d — the Schedule & Triggers tab (docs/decisions/0016 §4, Figma screen 5). The read-only summary
// is the default view; "Edit schedule" swaps it in place for the editor (ADR 0065 triggers, plus
// ADR 0071's pinnedVersion with each version's creation time).

export function ScheduleTab({ v, d, reload }: { v: ViewCtx; d: PipelineData; reload: () => void }) {
  const [editing, setEditing] = useState(false);
  const [saved, setSaved] = useState(false);
  if (editing) {
    return (
      <ScheduleForm
        v={v}
        d={d}
        onDone={(didSave) => {
          setEditing(false);
          setSaved(didSave);
          if (didSave) reload();
        }}
      />
    );
  }
  return (
    <ScheduleSummary
      v={v}
      d={d}
      saved={saved}
      onEdit={() => {
        setSaved(false);
        setEditing(true);
      }}
    />
  );
}

function ScheduleSummary({ v, d, saved, onEdit }: { v: ViewCtx; d: PipelineData; saved: boolean; onEdit: () => void }) {
  const p = d.pipeline;
  const s = p.schedule;
  const pinned = p.pinnedVersion ? d.versions.find((ver) => ver.version === p.pinnedVersion) : undefined;

  const rows: KV[] = [
    { label: "Trigger type", value: !s ? "None — manual only" : s.type === "cron" ? "Cron" : "Interval", mono: true },
    ...(s?.type === "cron"
      ? [
          {
            label: "Expression",
            value: (
              <>
                {s.cron} <span className="font-sans text-slate-500 dark:text-slate-400">— {describeSchedule(s)}</span>
              </>
            ),
            mono: true,
          },
          { label: "Timezone", value: s.timezone, mono: true },
        ]
      : s?.type === "interval"
        ? [{ label: "Every", value: describeInterval(s.seconds).replace(/^every /, ""), mono: true }]
        : []),
    ...(s ? [{ label: "Enabled", value: s.enabled ? "Yes" : "No — paused" }] : []),
    { label: "Next run", value: p.nextRunAt ? <span title={formatRelative(p.nextRunAt)}>{formatTime(p.nextRunAt)}</span> : "—", mono: true },
    {
      label: "Runs version",
      value: p.pinnedVersion ? `Pinned to v${p.pinnedVersion}${pinned ? ` — ${formatStamp(pinned.createdAt)}` : ""}` : "Always the latest saved version",
    },
    { label: "Concurrent runs", value: p.allowConcurrentRuns ? "Allowed" : "Not allowed — a new run is refused while one is active" },
    { label: "Platform access cap", value: p.roleCeiling === "editor" ? "Editor — can read and write storage and the catalog" : "Viewer — read only" },
  ];

  return (
    <div className="flex max-w-3xl flex-col gap-4">
      {saved && <Banner tone="success">Schedule saved.</Banner>}
      <Card
        title="Trigger"
        actions={
          v.canWrite ? (
            <Button size="sm" onClick={onEdit}>
              Edit schedule
            </Button>
          ) : undefined
        }
      >
        <KVRows rows={rows} />
      </Card>
      {s && s.enabled ? (
        <Banner tone="info">
          <strong>Scheduled.</strong> Runs {describeSchedule(s)}.{p.nextRunAt ? ` Next: ${formatTime(p.nextRunAt)}.` : ""}
        </Banner>
      ) : (
        <Banner tone="info">
          <strong>{s ? "Paused." : "Manual only."}</strong> This pipeline runs when someone presses Run now, above.
        </Banner>
      )}
      {!p.hasOwner && s && (
        <Banner tone="warn">This pipeline was scheduled before workload identity and has no owner, so its unattended runs get no platform access until its schedule is saved again.</Banner>
      )}
    </div>
  );
}

function ScheduleForm({ v, d, onDone }: { v: ViewCtx; d: PipelineData; onDone: (saved: boolean) => void }) {
  const p = d.pipeline;
  const [scheduled, setScheduled] = useState(p.schedule != null);
  const [shape, setShape] = useState<TriggerShape>(() =>
    p.schedule?.type === "interval"
      ? { type: "interval", seconds: p.schedule.seconds }
      : { type: "cron", cron: p.schedule?.cron ?? "0 9 * * *", timezone: p.schedule?.timezone ?? localTimezone() },
  );
  const [enabled, setEnabled] = useState(p.schedule?.enabled ?? true);
  const [concurrent, setConcurrent] = useState(p.allowConcurrentRuns);
  const [ceiling, setCeiling] = useState<"viewer" | "editor">(p.roleCeiling);
  const [pinned, setPinned] = useState<number | null>(p.pinnedVersion);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ message: string; field?: string } | null>(null);

  async function submit(ev: FormEvent) {
    ev.preventDefault();
    setBusy(true);
    setError(null);
    const schedule: Trigger | null = scheduled
      ? shape.type === "interval"
        ? { type: "interval", seconds: shape.seconds, enabled }
        : { type: "cron", cron: shape.cron.trim(), timezone: shape.timezone.trim(), enabled }
      : null;
    const body: PipelineScheduleUpdate = { schedule, allowConcurrentRuns: concurrent, roleCeiling: ceiling, pinnedVersion: pinned };
    try {
      await api.updateSchedule(v.api, p.id, body);
      onDone(true);
    } catch (err) {
      setError({ message: errorMessage(err), field: (err as { field?: string }).field });
      setBusy(false);
    }
  }

  const fieldError = (f: string) => (error?.field === f ? error.message : undefined);
  const routed = ["schedule.cron", "schedule.interval.seconds", "pinnedVersion"];

  return (
    <form onSubmit={submit} aria-label="Pipeline schedule" className="flex max-w-3xl flex-col gap-4">
      <Card title="Trigger">
        <fieldset disabled={busy} className="flex flex-col gap-4 p-4">
          <label className="flex items-center gap-2 text-sm font-medium text-slate-800 dark:text-slate-100">
            <input type="checkbox" checked={scheduled} onChange={(e) => setScheduled(e.target.checked)} />
            Run on a schedule
          </label>
          {scheduled ? (
            <>
              <ScheduleEditor value={shape} onChange={setShape} fallbackTimezone={localTimezone()} fieldError={fieldError} />
              <label className="flex items-center gap-2 text-sm text-slate-700 dark:text-slate-300">
                <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
                Schedule is active (untick to pause it without losing it)
              </label>
            </>
          ) : (
            <p className="text-xs text-slate-500 dark:text-slate-400">Without a schedule this pipeline runs only when someone presses Run now.</p>
          )}
        </fieldset>
      </Card>

      <Card title="What runs">
        <fieldset disabled={busy} className="flex flex-col gap-4 p-4">
          <Field
            id="pipeline-version-pin"
            label="Version"
            error={fieldError("pinnedVersion")}
            help="Follow the latest to pick up every saved version, or pin one so runs never change underneath you."
          >
            {(fp) => (
              <select {...fp} className={inputClass} value={pinned ?? ""} onChange={(e) => setPinned(e.target.value === "" ? null : Number(e.target.value))}>
                <option value="">Always the latest saved version</option>
                {d.versions.map((ver) => (
                  <option key={ver.version} value={ver.version}>
                    Pin to v{ver.version} — {formatStamp(ver.createdAt)}
                    {ver.notes ? ` — ${ver.notes}` : ""}
                  </option>
                ))}
              </select>
            )}
          </Field>
          <label className="flex items-center gap-2 text-sm text-slate-700 dark:text-slate-300">
            <input type="checkbox" checked={concurrent} onChange={(e) => setConcurrent(e.target.checked)} />
            Allow overlapping runs (otherwise a new run is refused while one is still active)
          </label>
          <Field
            id="pipeline-ceiling"
            label="Platform access cap"
            help="Tasks that opt in to platform access get a token capped at this role, and never above what the pipeline's owner currently holds."
          >
            {(fp) => (
              <select {...fp} className={inputClass} value={ceiling} onChange={(e) => setCeiling(e.target.value as "viewer" | "editor")}>
                <option value="editor">Editor — can read and write storage and the catalog</option>
                <option value="viewer">Viewer — read only</option>
              </select>
            )}
          </Field>
          {!p.hasOwner && scheduled && <Banner tone="warn">This pipeline has no owner yet (it predates workload identity). Saving this schedule makes you its owner.</Banner>}
        </fieldset>
      </Card>

      {error && (!error.field || !routed.includes(error.field)) && <Banner tone="error">{error.message}</Banner>}
      <div className="flex gap-2">
        <Button type="submit" variant="primary" disabled={busy}>
          {busy ? "Saving…" : "Save schedule"}
        </Button>
        <Button onClick={() => onDone(false)} disabled={busy}>
          Cancel
        </Button>
      </div>
    </form>
  );
}
