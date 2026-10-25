import { useEffect, useState, type FormEvent } from "react";
import { api } from "../../api/client";
import type { ViewCtx } from "../../context";
import { describeInterval, describeSchedule, formatRelative, formatStamp, formatTime, localTimezone } from "../../format";
import { errorMessage, useLoad } from "../../hooks";
import { ScheduleEditor, type TriggerShape } from "../../task/ScheduleEditor";
import type { EventTrigger, PipelineScheduleUpdate, Trigger } from "../../types";
import { Badge, Banner, Button, Card, Field, KVRows, inputClass, type KV } from "../../ui/primitives";
import type { PipelineData } from "./data";
import { DEFAULT_EVENT_TRIGGER, EventTriggerEditor, EventTriggerHelp, EventTriggerStatusLine, describeMatch, describeTiming } from "./EventTrigger";
import { OwnerBadge, OwnerPicker, ownerLabel, TakeOver } from "./RunsAs";

// S2d — the Schedule & Triggers tab (docs/decisions/0016 §4, Figma screen 5). The read-only summary
// is the default view; "Edit schedule" swaps it in place for the editor (ADR 0065 triggers, plus
// ADR 0071's pinnedVersion with each version's creation time). ADR 0123's event trigger sits beside
// the schedule, in its own card, on a backend that has it.

export function ScheduleTab({ v, d, reload, onDirty }: { v: ViewCtx; d: PipelineData; reload: () => void; onDirty?: (dirty: boolean) => void }) {
  const [editing, setEditing] = useState(false);
  const [saved, setSaved] = useState(false);
  if (editing) {
    return (
      <ScheduleForm
        v={v}
        d={d}
        onDirty={onDirty}
        onDone={(didSave) => {
          onDirty?.(false);
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
      reload={reload}
      saved={saved}
      onEdit={() => {
        setSaved(false);
        setEditing(true);
      }}
    />
  );
}

function ScheduleSummary({ v, d, reload, saved, onEdit }: { v: ViewCtx; d: PipelineData; reload: () => void; saved: boolean; onEdit: () => void }) {
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
    {
      label: "Runs as",
      value: (
        <div className="flex flex-col gap-2">
          <span className="flex flex-wrap items-center gap-2">
            {p.owner ? ownerLabel(p.owner) : "No one yet — the first person to save a schedule becomes its owner"}
            <OwnerBadge v={v} owner={p.owner} />
          </span>
          {v.role === "owner" ? <OwnerPicker v={v} p={p} onDone={reload} /> : <TakeOver v={v} p={p} onDone={reload} />}
        </div>
      ),
    },
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
      {p.eventTrigger !== undefined && <EventTriggerCard v={v} d={d} />}
      {s && s.enabled ? (
        <Banner tone="info">
          <strong>Scheduled.</strong> Runs {describeSchedule(s)}.{p.nextRunAt ? ` Next: ${formatTime(p.nextRunAt)}.` : ""}
        </Banner>
      ) : p.eventTrigger?.enabled ? (
        <Banner tone="info">
          <strong>Runs on events.</strong> {s ? "The schedule is paused." : "There is no schedule as a safety net."}
        </Banner>
      ) : (
        <Banner tone="info">
          <strong>{s ? "Paused." : "Manual only."}</strong> This pipeline runs when someone presses Run now, above.
        </Banner>
      )}
      {!p.hasOwner && s && (
        <Banner tone="warn">This pipeline was scheduled before workload identity and has no owner, so its scheduled runs can't start until someone takes it over.</Banner>
      )}
    </div>
  );
}

function EventTriggerCard({ v, d }: { v: ViewCtx; d: PipelineData }) {
  const t = d.pipeline.eventTrigger;
  const rows: KV[] = t
    ? [
        { label: "Watches", value: <ul className="flex flex-col gap-0.5">{t.match.map((m, i) => <li key={i}>{describeMatch(m)}</li>)}</ul> },
        { label: "When", value: describeTiming(t, d.pipeline.allowConcurrentRuns).join(" ") },
        { label: "Enabled", value: t.enabled ? "Yes" : "No — paused" },
      ]
    : [{ label: "Event trigger", value: "None" }];
  return (
    <Card title="Event trigger" label="Event trigger">
      <KVRows rows={rows} />
      {t && (
        <div className="flex flex-col gap-2 border-t border-slate-200 p-4 dark:border-slate-700">
          <EventTriggerStatusLine v={v} pipelineId={d.pipeline.id} />
          <EventTriggerHelp />
        </div>
      )}
    </Card>
  );
}

function ScheduleForm({ v, d, onDone, onDirty }: { v: ViewCtx; d: PipelineData; onDone: (saved: boolean) => void; onDirty?: (dirty: boolean) => void }) {
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
  // ADR 0123. A backend without event triggers sends no eventTrigger field, so it's never sent back.
  const hasEvents = p.eventTrigger !== undefined;
  const moduleConfig = useLoad(() => api.config(v.api), [v.api]);
  const eventsAvailable = moduleConfig.state.status !== "ready" || moduleConfig.state.data.eventTriggers?.available !== false;
  const [onEvents, setOnEvents] = useState(p.eventTrigger != null);
  const [eventTrigger, setEventTrigger] = useState<EventTrigger>(p.eventTrigger ?? DEFAULT_EVENT_TRIGGER);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ message: string; field?: string } | null>(null);

  function bodyOf(): PipelineScheduleUpdate {
    const schedule: Trigger | null = scheduled
      ? shape.type === "interval"
        ? { type: "interval", seconds: shape.seconds, enabled }
        : { type: "cron", cron: shape.cron.trim(), timezone: shape.timezone.trim(), enabled }
      : null;
    const body: PipelineScheduleUpdate = { schedule, allowConcurrentRuns: concurrent, roleCeiling: ceiling, pinnedVersion: pinned };
    if (hasEvents) body.eventTrigger = onEvents ? eventTrigger : null;
    return body;
  }
  // Unsaved: what Save would send differs from what the form opened with. Reported to the page, which
  // then asks before leaving this tab (switching tabs would drop the form).
  const [opened] = useState(() => JSON.stringify(bodyOf()));
  const dirty = JSON.stringify(bodyOf()) !== opened;
  useEffect(() => onDirty?.(dirty), [dirty, onDirty]);
  useEffect(() => () => onDirty?.(false), [onDirty]);

  async function submit(ev: FormEvent) {
    ev.preventDefault();
    setBusy(true);
    setError(null);
    const body = bodyOf();
    try {
      await api.updateSchedule(v.api, p.id, body);
      onDone(true);
    } catch (err) {
      setError({ message: errorMessage(err), field: (err as { field?: string }).field });
      setBusy(false);
    }
  }

  const fieldError = (f: string) => (error?.field === f ? error.message : undefined);
  const routed = ["schedule.cron", "schedule.interval.seconds", "pinnedVersion", "eventTrigger.debounceSeconds", "eventTrigger.maxWaitSeconds", "eventTrigger.minGapSeconds"];
  const routedHere = (f?: string) => !!f && (routed.includes(f) || /^eventTrigger\.match\[\d+\]\.storage\.backendId$/.test(f));

  return (
    <form onSubmit={submit} aria-label="Pipeline schedule" className="flex max-w-3xl flex-col gap-3">
      {/* The save actions first, so they're on screen without scrolling, with whether anything is unsaved. */}
      <div role="group" aria-label="Schedule actions" className="flex flex-wrap items-center gap-2">
        {/* Still enabled unchanged for a pipeline with no owner: saving is how it gets one. */}
        <Button type="submit" variant="primary" disabled={busy || (!dirty && p.hasOwner)}>
          {busy ? "Saving…" : "Save schedule"}
        </Button>
        <Button onClick={() => onDone(false)} disabled={busy}>
          {dirty ? "Discard changes" : "Close"}
        </Button>
        {dirty ? <Badge tone="amber">Unsaved changes</Badge> : <span className="text-xs text-slate-500 dark:text-slate-400">No changes yet</span>}
      </div>
      {error && !routedHere(error.field) && <Banner tone="error">{error.message}</Banner>}
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

      {hasEvents && (
        <Card title="Event trigger">
          <fieldset disabled={busy} className="flex flex-col gap-4 p-4">
            <label className="flex items-center gap-2 text-sm font-medium text-slate-800 dark:text-slate-100">
              <input type="checkbox" checked={onEvents} onChange={(e) => setOnEvents(e.target.checked)} />
              Run when files arrive or rows are written
            </label>
            {onEvents && !eventsAvailable && (
              <Banner tone="warn">This install has no event-bus credential, so an event trigger can be saved paused but can't be turned on.</Banner>
            )}
            {onEvents ? (
              <EventTriggerEditor v={v} value={eventTrigger} onChange={setEventTrigger} concurrent={concurrent} fieldError={fieldError} />
            ) : (
              <p className="text-xs text-slate-500 dark:text-slate-400">Runs only on the schedule above and Run now.</p>
            )}
          </fieldset>
        </Card>
      )}

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
          {!p.hasOwner && (scheduled || onEvents) && <Banner tone="warn">This pipeline has no owner yet (it predates workload identity). Saving this schedule makes you its owner.</Banner>}
        </fieldset>
      </Card>

    </form>
  );
}
