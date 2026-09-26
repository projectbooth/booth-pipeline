import { useState, type FormEvent } from "react";
import { api } from "../../api/client";
import type { ViewCtx } from "../../context";
import { describeInterval, formatDuration, formatRelative, formatStamp, formatTime } from "../../format";
import { errorMessage, useInterval, useLoad } from "../../hooks";
import type { PipelineTab } from "../../navigation";
import { isActive, runStart } from "../../runs";
import {
  Badge,
  Banner,
  Button,
  Card,
  Field,
  InlineConfirm,
  Link,
  Loaded,
  OverflowMenu,
  PageHeader,
  PlayIcon,
  StatStrip,
  StatusBadge,
  Tabs,
  inputClass,
  linkClass,
  type Stat,
} from "../../ui/primitives";
import { ConfigTab } from "./ConfigTab";
import { DagTab } from "./DagTab";
import { currentSpec, downloadYaml, loadPipeline, runnableVersion, useLatestRun, type LatestRun, type PipelineData } from "./data";
import { RunsTab } from "./RunsTab";
import { ScheduleTab } from "./ScheduleTab";
import { VersionsTab } from "./VersionsTab";
import { useResolved } from "../../dag/resolve";

// S2 — the pipeline detail page (docs/decisions/0016 §4): header, stat strip, Run now (on every
// tab — the gap that started ADR 0074's last round), and the five tabs.

const TABS: { key: PipelineTab; label: string }[] = [
  { key: "dag", label: "DAG" },
  { key: "runs", label: "Runs" },
  { key: "schedule", label: "Schedule & Triggers" },
  { key: "config", label: "Configuration" },
  { key: "versions", label: "Versions" },
];

export function PipelineDetail({ v, pipelineId, tab, version }: { v: ViewCtx; pipelineId: string; tab: PipelineTab; version?: number }) {
  const load = useLoad(() => loadPipeline(v.api, pipelineId, version), [v.api, pipelineId, version]);
  const latestRun = useLatestRun(v.api, pipelineId);
  return (
    <Loaded state={load.state} retry={load.reload}>
      {(d) => <PipelinePage v={v} d={d} tab={tab} latestRun={latestRun} reload={load.reload} />}
    </Loaded>
  );
}

function PipelinePage({ v, d, tab, latestRun, reload }: { v: ViewCtx; d: PipelineData; tab: PipelineTab; latestRun: LatestRun; reload: () => void }) {
  const p = d.pipeline;
  const [panel, setPanel] = useState<"none" | "details" | "delete">("none");
  const [deleting, setDeleting] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const refs = currentSpec(d);
  // Resolved once here and shared by the DAG and Configuration tabs.
  const { resolved } = useResolved(v.api, refs);

  const exportVersion = d.viewed?.version ?? d.latest?.version ?? null;

  async function exportYaml() {
    if (exportVersion === null) return;
    setActionError(null);
    try {
      await downloadYaml(v.api, p, exportVersion);
    } catch (err) {
      setActionError(`Export failed: ${errorMessage(err)}`);
    }
  }

  async function remove() {
    setDeleting(true);
    setActionError(null);
    try {
      await api.deletePipeline(v.api, p.id);
      v.go({ name: "pipelines" });
    } catch (err) {
      setActionError(`Could not delete: ${errorMessage(err)}`);
      setDeleting(false);
    }
  }

  const menu = [
    { label: exportVersion ? `Export v${exportVersion} as YAML` : "Export as YAML (save a version first)", onSelect: exportYaml, disabled: exportVersion === null },
    ...(v.canWrite
      ? [
          { label: "Edit details…", onSelect: () => setPanel("details") },
          { label: "Delete pipeline…", onSelect: () => setPanel("delete"), danger: true },
        ]
      : []),
  ];

  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        crumbs={
          <>
            <Link href={v.href({ name: "pipelines" })} onNavigate={v.goPath} className="hover:text-slate-800 dark:hover:text-slate-200">
              ← Pipelines
            </Link>
            <span className="mx-1.5">/</span>
            <span className="text-slate-800 dark:text-slate-200">{p.name}</span>
          </>
        }
        title={p.name}
        titleExtra={
          <>
            <StatusBadge status={latestRun.run ? latestRun.run.status : "never"} />
            {d.draftDiffers && (
              <Badge tone="sky" title="Saved with plain Save; not yet a numbered version">
                {d.latest ? `Draft ahead of v${d.latest.version}` : "Draft, never saved as a version"}
              </Badge>
            )}
          </>
        }
        subtitle={p.description || undefined}
        actions={
          <>
            {v.canWrite && <RunNow v={v} d={d} />}
            <OverflowMenu label="More pipeline actions" items={menu} />
          </>
        }
      />

      {actionError && <Banner tone="error">{actionError}</Banner>}
      {panel === "details" && (
        <DetailsCard
          v={v}
          d={d}
          onDone={(changed) => {
            setPanel("none");
            if (changed) reload();
          }}
        />
      )}
      {panel === "delete" && (
        <InlineConfirm
          message={
            <>
              Delete <strong>{p.name}</strong>, all {d.versions.length} of its versions and its run history? Any active run is cancelled.
            </>
          }
          confirmLabel="Delete pipeline"
          busyLabel="Deleting…"
          busy={deleting}
          onConfirm={remove}
          onCancel={() => setPanel("none")}
        />
      )}

      <StatStrip label="Pipeline summary" stats={summaryStats(v, d, latestRun, refs.length)} />

      <div className="flex flex-col gap-4">
        <Tabs label="Pipeline views" tabs={TABS} active={tab} hrefFor={(t) => v.href({ name: "pipeline", id: p.id, tab: t })} onNavigate={v.goPath} />
        {tab === "dag" && <DagTab v={v} d={d} refs={refs} resolved={resolved} latestRun={latestRun} />}
        {tab === "runs" && <RunsTab v={v} pipeline={p} />}
        {tab === "schedule" && <ScheduleTab v={v} d={d} />}
        {tab === "config" && <ConfigTab v={v} d={d} refs={refs} resolved={resolved} />}
        {tab === "versions" && <VersionsTab v={v} d={d} />}
      </div>
    </div>
  );
}

function summaryStats(v: ViewCtx, d: PipelineData, latestRun: LatestRun, taskCount: number): Stat[] {
  const p = d.pipeline;
  const run = latestRun.run;
  const schedule = !p.schedule
    ? "Manual only"
    : `${p.schedule.type === "cron" ? `${p.schedule.cron} (${p.schedule.timezone})` : describeInterval(p.schedule.seconds)}${p.schedule.enabled ? "" : " · paused"}`;
  return [
    {
      label: "Last run",
      value: run ? (
        <Link href={v.href({ name: "run", id: run.id })} onNavigate={v.goPath} className={linkClass}>
          {formatStamp(runStart(run))}
        </Link>
      ) : (
        "—"
      ),
      title: run ? `${formatTime(runStart(run))} (${formatRelative(runStart(run))})` : undefined,
    },
    { label: "Duration", value: run ? <LiveDuration start={run.startedAt} end={run.finishedAt} active={isActive(run)} /> : "—" },
    { label: "Next run", value: p.nextRunAt ? formatStamp(p.nextRunAt) : p.schedule ? "—" : "Manual only", title: p.nextRunAt ? formatRelative(p.nextRunAt) : undefined },
    { label: "Schedule", value: schedule, title: schedule },
    { label: "Owner", value: p.createdBy, mono: false },
    { label: "Tasks", value: String(taskCount) },
  ];
}

function LiveDuration({ start, end, active }: { start: string | null; end: string | null; active: boolean }) {
  const [now, setNow] = useState(Date.now());
  useInterval(() => setNow(Date.now()), 1000, active);
  if (!start) return <>—</>;
  return <>{formatDuration(start, end, now)}</>;
}

/** ▶ Run now, plus the one line of small print that says exactly which version it runs — the
 *  backend runs the pin or the latest SAVED version, never the draft (service.start_run), and
 *  without saying so a plain Save followed by Run now would silently run stale wiring. */
function RunNow({ v, d }: { v: ViewCtx; d: PipelineData }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const target = runnableVersion(d.pipeline);

  async function run() {
    setBusy(true);
    setError(null);
    try {
      const r = await api.runPipeline(v.api, d.pipeline.id);
      v.go({ name: "run", id: r.id });
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  }

  const note = !target
    ? "Nothing to run yet: save a version first."
    : `Runs v${target.version}${target.pinned ? " (pinned)" : " (latest saved version)"}${d.draftDiffers ? " — your draft changes aren't in it" : ""}`;

  return (
    <div className="flex flex-col items-end gap-1">
      <Button variant="primary" onClick={run} disabled={!target || busy} aria-describedby="run-now-note">
        <PlayIcon /> {busy ? "Starting…" : "Run now"}
      </Button>
      <p id="run-now-note" className={`max-w-xs text-right text-xs ${d.draftDiffers && target ? "text-amber-700 dark:text-amber-400" : "text-slate-500 dark:text-slate-400"}`}>
        {note}
      </p>
      {error && (
        <p role="alert" className="max-w-xs text-right text-xs text-red-600 dark:text-red-400">
          {error}
        </p>
      )}
    </div>
  );
}

function DetailsCard({ v, d, onDone }: { v: ViewCtx; d: PipelineData; onDone: (changed: boolean) => void }) {
  const [name, setName] = useState(d.pipeline.name);
  const [description, setDescription] = useState(d.pipeline.description);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ message: string; field?: string } | null>(null);

  async function submit(ev: FormEvent) {
    ev.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.updatePipeline(v.api, d.pipeline.id, name.trim(), description.trim());
      onDone(true);
    } catch (err) {
      setError({ message: errorMessage(err), field: (err as { field?: string }).field });
      setBusy(false);
    }
  }

  return (
    <Card title="Pipeline details">
      <form onSubmit={submit} aria-label="Pipeline details" className="flex flex-col gap-3 p-4">
        <div className="grid gap-3 sm:grid-cols-2">
          <Field id="pipeline-name" label="Name" required error={error?.field === "name" ? error.message : undefined}>
            {(p) => <input {...p} className={inputClass} value={name} maxLength={200} onChange={(e) => setName(e.target.value)} />}
          </Field>
          <Field id="pipeline-description" label="Description">
            {(p) => <input {...p} className={inputClass} value={description} maxLength={4000} onChange={(e) => setDescription(e.target.value)} />}
          </Field>
        </div>
        {error && error.field !== "name" && <Banner tone="error">{error.message}</Banner>}
        <div className="flex gap-2">
          <Button type="submit" variant="primary" disabled={busy || name.trim() === ""}>
            {busy ? "Saving…" : "Save details"}
          </Button>
          <Button onClick={() => onDone(false)} disabled={busy}>
            Cancel
          </Button>
        </div>
      </form>
    </Card>
  );
}
