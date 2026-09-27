import { useMemo, useState, type FormEvent } from "react";
import { api } from "../api/client";
import type { ViewCtx } from "../context";
import { formatDuration, formatRelative, formatStamp, formatTime } from "../format";
import { errorMessage, useDebounced, useInterval, useLoad } from "../hooks";
import { isActive, latestRunsFor, runStart } from "../runs";
import type { Pipeline, Run } from "../types";
import {
  Banner,
  Button,
  Card,
  EmptyState,
  Field,
  InlineConfirm,
  Link,
  Loaded,
  OverflowMenu,
  PageHeader,
  PlayIcon,
  Skeleton,
  StatusBadge,
  inputClass,
  linkClass,
  rowHoverClass,
  tableClass,
  tbodyClass,
  tdClass,
  thClass,
  theadClass,
  type AnyStatus,
} from "../ui/primitives";
import { TriggerSummary } from "./pipeline/TriggerSummary";

// S1 — the Pipelines list (docs/decisions/0016 §4, Figma screen 1). One row per pipeline entity;
// version history lives on the pipeline itself (S2f), never here.

type Filter = "all" | "running" | "failed" | "succeeded" | "never";

const FILTERS: { key: Filter; label: string }[] = [
  { key: "all", label: "All" },
  { key: "running", label: "Running" },
  { key: "failed", label: "Failed" },
  { key: "succeeded", label: "Succeeded" },
  { key: "never", label: "Never run" },
];

function statusOf(run: Run | null): AnyStatus {
  return run ? run.status : "never";
}

function matches(filter: Filter, run: Run | null): boolean {
  switch (filter) {
    case "all":
      return true;
    case "running":
      return isActive(run);
    case "never":
      return run === null;
    default:
      return run?.status === filter;
  }
}

export function PipelineList({ v }: { v: ViewCtx }) {
  const [query, setQuery] = useState("");
  const q = useDebounced(query.trim(), 250);
  const [filter, setFilter] = useState<Filter>("all");
  const [creating, setCreating] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [starting, setStarting] = useState<string | null>(null);

  const load = useLoad(async () => {
    const page = await api.listPipelines(v.api, q);
    const latest = await latestRunsFor(v.api, page.items.map((p) => p.id));
    return { pipelines: page.items, latest };
  }, [v.api, q]);

  const data = load.state.status === "ready" ? load.state.data : null;
  const anyActive = !!data && Object.values(data.latest).some(isActive);
  useInterval(load.reload, 10_000, anyActive);

  const counts = useMemo(() => {
    const c: Record<Filter, number> = { all: 0, running: 0, failed: 0, succeeded: 0, never: 0 };
    if (!data) return c;
    for (const p of data.pipelines) for (const f of FILTERS) if (matches(f.key, data.latest[p.id] ?? null)) c[f.key]++;
    return c;
  }, [data]);

  async function runNow(p: Pipeline) {
    setActionError(null);
    setStarting(p.id);
    try {
      const run = await api.runPipeline(v.api, p.id);
      v.go({ name: "run", id: run.id });
    } catch (err) {
      setActionError(`Could not start "${p.name}": ${errorMessage(err)}`);
      setStarting(null);
    }
  }

  async function remove(p: Pipeline) {
    setDeleting(true);
    setActionError(null);
    try {
      await api.deletePipeline(v.api, p.id);
      setConfirmDelete(null);
      load.reload();
    } catch (err) {
      setActionError(`Could not delete "${p.name}": ${errorMessage(err)}`);
    } finally {
      setDeleting(false);
    }
  }

  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        title="Pipelines"
        subtitle="Orchestrate tasks, schedule runs, and track their history."
        actions={
          v.canWrite && !creating ? (
            <Button variant="primary" onClick={() => setCreating(true)}>
              + New pipeline
            </Button>
          ) : undefined
        }
      />

      {creating && <NewPipelineCard v={v} onCancel={() => setCreating(false)} />}

      <div className="flex flex-wrap items-center gap-3">
        <input
          type="search"
          aria-label="Search pipelines"
          placeholder="Search pipelines…"
          className={`${inputClass} max-w-sm`}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <div role="group" aria-label="Filter by last run status" className="inline-flex flex-wrap rounded-md border border-slate-200 p-0.5 dark:border-slate-700">
          {FILTERS.map((f) => (
            <button
              key={f.key}
              type="button"
              aria-pressed={filter === f.key}
              onClick={() => setFilter(f.key)}
              className={`rounded px-2.5 py-1 text-sm ${
                filter === f.key
                  ? "bg-slate-100 font-medium text-slate-900 dark:bg-slate-800 dark:text-slate-50"
                  : "text-slate-500 hover:text-slate-800 dark:text-slate-400 dark:hover:text-slate-200"
              }`}
            >
              {f.label} <span className="ml-0.5 text-xs text-slate-400">{data ? counts[f.key] : ""}</span>
            </button>
          ))}
        </div>
      </div>

      {actionError && <Banner tone="error">{actionError}</Banner>}

      <Loaded state={load.state} retry={load.reload} skeleton={<TableSkeleton />}>
        {({ pipelines, latest }) => {
          if (pipelines.length === 0) {
            return q ? (
              <EmptyState title={`No pipelines match "${q}"`} />
            ) : (
              <EmptyState
                title="No pipelines yet"
                action={
                  v.canWrite && !creating ? (
                    <Button variant="primary" onClick={() => setCreating(true)}>
                      + New pipeline
                    </Button>
                  ) : undefined
                }
              >
                {v.canWrite ? "Create one to start building a DAG of tasks." : "Someone with edit access needs to create one."}
              </EmptyState>
            );
          }
          const shown = pipelines.filter((p) => matches(filter, latest[p.id] ?? null));
          if (shown.length === 0) return <EmptyState title="No pipelines match this filter" />;
          return (
            <div className="relative overflow-x-auto">
              <table className={tableClass}>
                <thead className={theadClass}>
                  <tr>
                    <th className={thClass}>Name</th>
                    <th className={thClass}>Status</th>
                    <th className={`${thClass} hidden lg:table-cell`}>Trigger</th>
                    <th className={thClass}>Last run</th>
                    <th className={`${thClass} hidden lg:table-cell`}>Duration</th>
                    <th className={`${thClass} hidden lg:table-cell`}>Next run</th>
                    <th className={`${thClass} hidden xl:table-cell`}>Owner</th>
                    <th className={thClass}>
                      <span className="sr-only">Actions</span>
                    </th>
                  </tr>
                </thead>
                <tbody className={tbodyClass}>
                  {shown.map((p) => {
                    const run = latest[p.id] ?? null;
                    const href = v.href({ name: "pipeline", id: p.id });
                    const neverSaved = p.latestVersion === 0;
                    return (
                      <PipelineRow
                        key={p.id}
                        p={p}
                        run={run}
                        href={href}
                        v={v}
                        neverSaved={neverSaved}
                        starting={starting === p.id}
                        confirming={confirmDelete === p.id}
                        deleting={deleting}
                        onRun={() => runNow(p)}
                        onAskDelete={() => setConfirmDelete(p.id)}
                        onCancelDelete={() => setConfirmDelete(null)}
                        onDelete={() => remove(p)}
                      />
                    );
                  })}
                </tbody>
              </table>
            </div>
          );
        }}
      </Loaded>
    </div>
  );
}

function PipelineRow({
  p,
  run,
  href,
  v,
  neverSaved,
  starting,
  confirming,
  deleting,
  onRun,
  onAskDelete,
  onCancelDelete,
  onDelete,
}: {
  p: Pipeline;
  run: Run | null;
  href: string;
  v: ViewCtx;
  neverSaved: boolean;
  starting: boolean;
  confirming: boolean;
  deleting: boolean;
  onRun: () => void;
  onAskDelete: () => void;
  onCancelDelete: () => void;
  onDelete: () => void;
}) {
  return (
    <>
      <tr className={`cursor-pointer ${rowHoverClass}`} onClick={(e) => !(e.target as HTMLElement).closest("a,button") && v.goPath(href)}>
        <td className={`${tdClass} min-w-48`}>
          <Link href={href} onNavigate={v.goPath} className="font-medium text-slate-900 hover:underline dark:text-slate-50">
            {p.name}
          </Link>
          {p.description && <p className="max-w-xs truncate text-xs text-slate-500 dark:text-slate-400">{p.description}</p>}
        </td>
        <td className={tdClass}>
          <StatusBadge status={statusOf(run)} />
        </td>
        <td className={`${tdClass} hidden lg:table-cell`}>
          <TriggerSummary schedule={p.schedule} compact />
        </td>
        <td className={`${tdClass} whitespace-nowrap font-mono text-xs`} title={run ? `${formatTime(runStart(run))} (${formatRelative(runStart(run))})` : undefined}>
          {run ? (
            <Link href={v.href({ name: "run", id: run.id })} onNavigate={v.goPath} className={linkClass}>
              {formatStamp(runStart(run))}
            </Link>
          ) : (
            "—"
          )}
        </td>
        <td className={`${tdClass} hidden whitespace-nowrap font-mono text-xs lg:table-cell`}>{run && run.finishedAt ? formatDuration(run.startedAt, run.finishedAt) : "—"}</td>
        <td className={`${tdClass} hidden whitespace-nowrap font-mono text-xs lg:table-cell`} title={p.nextRunAt ? formatRelative(p.nextRunAt) : undefined}>
          {p.nextRunAt ? formatStamp(p.nextRunAt) : "—"}
        </td>
        <td className={`${tdClass} hidden max-w-40 truncate text-slate-600 xl:table-cell dark:text-slate-300`}>{p.createdBy}</td>
        <td className={`${tdClass} whitespace-nowrap text-right`}>
          {v.canWrite && (
            <span className="inline-flex items-center gap-2">
              <Button
                size="sm"
                onClick={onRun}
                disabled={neverSaved || starting}
                title={neverSaved ? "Save a version first — Run now runs the latest saved version" : `Run ${p.pinnedVersion ? `v${p.pinnedVersion} (pinned)` : `v${p.latestVersion}`} now`}
                aria-label={`Run ${p.name} now`}
              >
                {starting ? "Starting…" : (
                  <>
                    <PlayIcon /> Run
                  </>
                )}
              </Button>
              <OverflowMenu label={`More actions for ${p.name}`} items={[{ label: "Delete pipeline…", danger: true, onSelect: onAskDelete }]} />
            </span>
          )}
        </td>
      </tr>
      {confirming && (
        <tr>
          <td colSpan={8} className="px-3 py-2">
            <InlineConfirm
              message={
                <>
                  Delete <strong>{p.name}</strong>, all its versions and its run history? Any active run is cancelled.
                </>
              }
              confirmLabel="Delete pipeline"
              busyLabel="Deleting…"
              busy={deleting}
              onConfirm={onDelete}
              onCancel={onCancelDelete}
            />
          </td>
        </tr>
      )}
    </>
  );
}

function NewPipelineCard({ v, onCancel }: { v: ViewCtx; onCancel: () => void }) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ message: string; field?: string } | null>(null);

  async function submit(ev: FormEvent) {
    ev.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const p = await api.createPipeline(v.api, name.trim(), description.trim());
      v.go({ name: "pipeline", id: p.id });
    } catch (err) {
      setError({ message: errorMessage(err), field: (err as { field?: string }).field });
      setBusy(false);
    }
  }

  return (
    <Card title="New pipeline">
      <form onSubmit={submit} aria-label="New pipeline" className="flex flex-col gap-3 p-4">
        <div className="grid gap-3 sm:grid-cols-2">
          <Field id="new-pipeline-name" label="Name" required error={error?.field === "name" ? error.message : undefined}>
            {(p) => <input {...p} autoFocus className={inputClass} value={name} maxLength={200} onChange={(e) => setName(e.target.value)} />}
          </Field>
          <Field id="new-pipeline-description" label="Description">
            {(p) => <input {...p} className={inputClass} value={description} maxLength={4000} onChange={(e) => setDescription(e.target.value)} />}
          </Field>
        </div>
        {error && error.field !== "name" && <Banner tone="error">{error.message}</Banner>}
        <div className="flex gap-2">
          <Button type="submit" variant="primary" disabled={busy || name.trim() === ""}>
            {busy ? "Creating…" : "Create and open"}
          </Button>
          <Button onClick={onCancel} disabled={busy}>
            Cancel
          </Button>
        </div>
      </form>
    </Card>
  );
}

function TableSkeleton() {
  return (
    <div role="status" aria-label="Loading" className="flex flex-col gap-3">
      {Array.from({ length: 5 }, (_, i) => (
        <Skeleton key={i} className="h-10 w-full" />
      ))}
    </div>
  );
}
