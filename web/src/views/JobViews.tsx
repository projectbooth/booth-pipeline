import { useEffect, useState, type FormEvent } from "react";
import { api } from "../api/client";
import { Banner, Button, EmptyState, Field, Link, Loaded, PageHeader, StatusChip, inputClass, linkClass, tdClass, thClass } from "../components/ui";
import type { ViewCtx } from "../context";
import { COMMON_TIMEZONES, CRON_PRESETS, describeSchedule, formatDuration, formatTime, localTimezone } from "../format";
import { errorMessage, useLoad } from "../hooks";
import type { Backoff, Job, JobInput, Pipeline, RetryPolicy, Schedule } from "../types";

// Jobs: a Pipeline turned into something you can run on demand or on a schedule. A Job carries
// its own schedule, default retry policy and run history, and either follows the pipeline's latest
// saved version or is pinned to one.

export function JobList({ v }: { v: ViewCtx }) {
  const load = useLoad(async () => {
    const [jobs, pipelines] = await Promise.all([api.listJobs(v.api), api.listPipelines(v.api)]);
    return { jobs: jobs.items, pipelines: new Map(pipelines.items.map((p) => [p.id, p])) };
  }, [v.api]);
  const [runError, setRunError] = useState<string | null>(null);

  async function runNow(job: Job) {
    setRunError(null);
    try {
      const run = await api.runJob(v.api, job.id);
      v.go({ name: "run", id: run.id });
    } catch (err) {
      setRunError(errorMessage(err));
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Jobs"
        subtitle="A job runs a pipeline on demand or on a schedule, with its own retry policy and run history."
        actions={v.canWrite ? <Link href={v.href({ name: "job-new" })} onNavigate={v.goPath} className="rounded-md bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-500">New job</Link> : undefined}
      />
      {runError && <Banner tone="error">{runError}</Banner>}
      <Loaded state={load.state} retry={load.reload}>
        {({ jobs, pipelines }) =>
          jobs.length === 0 ? (
            <EmptyState title="No jobs yet">
              {v.canWrite ? "Turn a saved pipeline into a job to run it on demand or on a schedule." : "Someone with edit access needs to create one."}
            </EmptyState>
          ) : (
            <div className="overflow-x-auto rounded-md border border-slate-200 dark:border-slate-700">
              <table className="w-full">
                <thead className="border-b border-slate-200 bg-slate-50 dark:border-slate-700 dark:bg-slate-900">
                  <tr>
                    <th className={thClass}>Job</th>
                    <th className={thClass}>Pipeline</th>
                    <th className={thClass}>Schedule</th>
                    <th className={thClass}>Next run</th>
                    <th className={thClass}>
                      <span className="sr-only">Actions</span>
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-200 dark:divide-slate-700">
                  {jobs.map((j) => (
                    <tr key={j.id}>
                      <td className={tdClass}>
                        <Link href={v.href({ name: "job", id: j.id })} onNavigate={v.goPath} className={`${linkClass} font-medium`}>
                          {j.name}
                        </Link>
                      </td>
                      <td className={tdClass}>
                        {pipelines.get(j.pipelineId)?.name ?? "—"}{" "}
                        <span className="text-xs text-slate-500 dark:text-slate-400">{j.pipelineVersion ? `pinned to v${j.pipelineVersion}` : "latest"}</span>
                      </td>
                      <td className={tdClass}>{describeSchedule(j.schedule)}</td>
                      <td className={tdClass}>{formatTime(j.nextRunAt)}</td>
                      <td className={`${tdClass} text-right`}>
                        {v.canWrite && (
                          <Button onClick={() => runNow(j)} aria-label={`Run ${j.name} now`}>
                            Run now
                          </Button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )
        }
      </Loaded>
    </div>
  );
}

export function JobNew({ v, pipelineId }: { v: ViewCtx; pipelineId?: string }) {
  const load = useLoad(() => api.listPipelines(v.api), [v.api]);
  return (
    <div className="flex max-w-2xl flex-col gap-4">
      <PageHeader title="New job" subtitle="Choose a pipeline, then decide when and how it runs." />
      <Loaded state={load.state} retry={load.reload}>
        {(page) => {
          const usable = page.items.filter((p) => p.latestVersion > 0);
          return usable.length === 0 ? (
            <EmptyState title="Nothing to run yet">A job needs a pipeline with at least one saved version. Draw one in the builder and save it first.</EmptyState>
          ) : (
            <JobForm v={v} pipelines={usable} initial={null} pipelineId={pipelineId} />
          );
        }}
      </Loaded>
    </div>
  );
}

export function JobDetail({ v, jobId }: { v: ViewCtx; jobId: string }) {
  const load = useLoad(async () => {
    const [job, pipelines, runs] = await Promise.all([api.getJob(v.api, jobId), api.listPipelines(v.api), api.listRuns(v.api, jobId)]);
    return { job, pipelines: pipelines.items.filter((p) => p.latestVersion > 0 || p.id === job.pipelineId), runs: runs.items };
  }, [v.api, jobId]);
  const [error, setError] = useState<string | null>(null);

  async function runNow() {
    setError(null);
    try {
      const run = await api.runJob(v.api, jobId);
      v.go({ name: "run", id: run.id });
    } catch (err) {
      setError(errorMessage(err));
      load.reload();
    }
  }

  async function remove(name: string) {
    if (!window.confirm(`Delete the job "${name}" and its entire run history? This cannot be undone.`)) return;
    try {
      await api.deleteJob(v.api, jobId);
      v.go({ name: "jobs" });
    } catch (err) {
      setError(errorMessage(err));
    }
  }

  return (
    <Loaded state={load.state} retry={load.reload}>
      {({ job, pipelines, runs }) => (
        <div className="flex flex-col gap-6">
          <PageHeader
            title={job.name}
            subtitle={describeSchedule(job.schedule)}
            actions={
              v.canWrite ? (
                <>
                  <Button variant="primary" onClick={runNow}>
                    Run now
                  </Button>
                  <Button variant="danger" onClick={() => remove(job.name)}>
                    Delete
                  </Button>
                </>
              ) : undefined
            }
          />
          {error && <Banner tone="error">{error}</Banner>}

          <section aria-label="Run history" className="flex flex-col gap-2">
            <h3 className="text-sm font-semibold text-slate-800 dark:text-slate-100">Run history</h3>
            {runs.length === 0 ? (
              <EmptyState title="No runs yet">{v.canWrite ? "Use “Run now”, or wait for the schedule." : "Nothing has run yet."}</EmptyState>
            ) : (
              <div className="overflow-x-auto rounded-md border border-slate-200 dark:border-slate-700">
                <table className="w-full">
                  <thead className="border-b border-slate-200 bg-slate-50 dark:border-slate-700 dark:bg-slate-900">
                    <tr>
                      <th className={thClass}>Started</th>
                      <th className={thClass}>Status</th>
                      <th className={thClass}>Version</th>
                      <th className={thClass}>Trigger</th>
                      <th className={thClass}>Duration</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-200 dark:divide-slate-700">
                    {runs.map((r) => (
                      <tr key={r.id}>
                        <td className={tdClass}>
                          <Link href={v.href({ name: "run", id: r.id })} onNavigate={v.goPath} className={linkClass}>
                            {formatTime(r.startedAt ?? r.createdAt)}
                          </Link>
                        </td>
                        <td className={tdClass}>
                          <StatusChip status={r.status} />
                        </td>
                        <td className={tdClass}>v{r.pipelineVersion}</td>
                        <td className={tdClass}>
                          {r.trigger === "schedule" ? "schedule" : `manual (${r.triggeredBy})`}
                        </td>
                        <td className={tdClass}>{formatDuration(r.startedAt, r.finishedAt)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <section aria-label="Job settings" className="flex max-w-2xl flex-col gap-3">
            <h3 className="text-sm font-semibold text-slate-800 dark:text-slate-100">Settings</h3>
            <JobForm v={v} pipelines={pipelines} initial={job} onSaved={load.reload} />
          </section>
        </div>
      )}
    </Loaded>
  );
}

function JobForm({
  v,
  pipelines,
  initial,
  pipelineId,
  onSaved,
}: {
  v: ViewCtx;
  pipelines: Pipeline[];
  initial: Job | null;
  pipelineId?: string;
  onSaved?: () => void;
}) {
  const readOnly = !v.canWrite;
  const [name, setName] = useState(initial?.name ?? "");
  const [pid, setPid] = useState(initial?.pipelineId ?? pipelineId ?? pipelines[0]?.id ?? "");
  const [pinned, setPinned] = useState<number | null>(initial?.pipelineVersion ?? null);
  const [scheduled, setScheduled] = useState(initial?.schedule != null);
  const [cron, setCron] = useState(initial?.schedule?.cron ?? "0 9 * * *");
  const [timezone, setTimezone] = useState(initial?.schedule?.timezone ?? localTimezone());
  const [enabled, setEnabled] = useState(initial?.schedule?.enabled ?? true);
  const [retryOn, setRetryOn] = useState(initial?.retry != null && initial.retry.maxRetries > 0);
  const [retry, setRetry] = useState<RetryPolicy>(initial?.retry ?? { maxRetries: 2, delaySeconds: 30, backoff: "exponential" });
  const [concurrent, setConcurrent] = useState(initial?.allowConcurrentRuns ?? false);
  const [ceiling, setCeiling] = useState<"viewer" | "editor">(initial?.roleCeiling ?? "editor");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ message: string; field?: string } | null>(null);
  const [saved, setSaved] = useState(false);
  const [versions, setVersions] = useState<{ version: number }[]>([]);

  // The versions this pipeline can be pinned to.
  useEffect(() => {
    if (!pid) return;
    let alive = true;
    api.listVersions(v.api, pid).then(
      (p) => alive && setVersions(p.items),
      () => alive && setVersions([]),
    );
    return () => {
      alive = false;
    };
  }, [v.api, pid]);

  async function submit(ev: FormEvent) {
    ev.preventDefault();
    setBusy(true);
    setError(null);
    setSaved(false);
    const schedule: Schedule | null = scheduled ? { cron: cron.trim(), timezone: timezone.trim(), enabled } : null;
    const body: JobInput = {
      name: name.trim(),
      pipelineId: pid,
      pipelineVersion: pinned,
      schedule,
      retry: retryOn ? retry : null,
      allowConcurrentRuns: concurrent,
      roleCeiling: ceiling,
    };
    try {
      if (initial) {
        await api.updateJob(v.api, initial.id, body);
        setSaved(true);
        onSaved?.();
      } else {
        const job = await api.createJob(v.api, body);
        v.go({ name: "job", id: job.id });
      }
    } catch (err) {
      setError({ message: errorMessage(err), field: (err as { field?: string }).field });
    } finally {
      setBusy(false);
    }
  }

  const fieldError = (f: string) => (error?.field === f ? error.message : undefined);

  return (
    <form onSubmit={submit} aria-label={initial ? "Job settings" : "New job"} className="flex flex-col gap-4">
      <fieldset disabled={readOnly || busy} className="flex flex-col gap-4">
        <Field id="job-name" label="Name" required error={fieldError("name")}>
          {(p) => <input {...p} className={inputClass} value={name} maxLength={200} onChange={(e) => setName(e.target.value)} />}
        </Field>

        <div className="grid gap-3 sm:grid-cols-2">
          <Field id="job-pipeline" label="Pipeline" required>
            {(p) => (
              <select
                {...p}
                className={inputClass}
                value={pid}
                onChange={(e) => {
                  setPid(e.target.value);
                  setPinned(null);
                }}
              >
                {pipelines.map((pl) => (
                  <option key={pl.id} value={pl.id}>
                    {pl.name}
                  </option>
                ))}
              </select>
            )}
          </Field>
          <Field id="job-version" label="Version" error={fieldError("pipelineVersion")} help="Follow the latest to pick up every save, or pin one so edits never change this job.">
            {(p) => (
              <select {...p} className={inputClass} value={pinned ?? ""} onChange={(e) => setPinned(e.target.value === "" ? null : Number(e.target.value))}>
                <option value="">Always the latest saved version</option>
                {versions.map((ver) => (
                  <option key={ver.version} value={ver.version}>
                    Pin to v{ver.version}
                  </option>
                ))}
              </select>
            )}
          </Field>
        </div>

        <section aria-label="Schedule" className="flex flex-col gap-2 rounded-md border border-slate-200 p-3 dark:border-slate-700">
          <label className="flex items-center gap-2 text-sm font-medium text-slate-800 dark:text-slate-100">
            <input type="checkbox" checked={scheduled} onChange={(e) => setScheduled(e.target.checked)} />
            Run on a schedule
          </label>
          {scheduled ? (
            <>
              <div className="grid gap-3 sm:grid-cols-2">
                <Field id="job-cron" label="Cron expression" required error={fieldError("schedule")} help="Five fields: minute hour day-of-month month day-of-week.">
                  {(p) => <input {...p} className={`${inputClass} font-mono`} value={cron} onChange={(e) => setCron(e.target.value)} list="cron-presets" />}
                </Field>
                <Field id="job-tz" label="Timezone" help="Evaluated in this timezone, so 09:00 stays 09:00 across daylight saving.">
                  {(p) => <input {...p} className={inputClass} value={timezone} onChange={(e) => setTimezone(e.target.value)} list="timezones" />}
                </Field>
              </div>
              <datalist id="cron-presets">
                {CRON_PRESETS.map((c) => (
                  <option key={c.cron} value={c.cron}>
                    {c.label}
                  </option>
                ))}
              </datalist>
              <datalist id="timezones">
                {COMMON_TIMEZONES.map((t) => (
                  <option key={t} value={t} />
                ))}
              </datalist>
              <label className="flex items-center gap-2 text-sm text-slate-700 dark:text-slate-300">
                <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
                Schedule is active (untick to pause without losing it)
              </label>
            </>
          ) : (
            <p className="text-xs text-slate-500 dark:text-slate-400">Without a schedule this job only runs when you press "Run now".</p>
          )}
        </section>

        <section aria-label="Default retry policy" className="flex flex-col gap-2 rounded-md border border-slate-200 p-3 dark:border-slate-700">
          <label className="flex items-center gap-2 text-sm font-medium text-slate-800 dark:text-slate-100">
            <input type="checkbox" checked={retryOn} onChange={(e) => setRetryOn(e.target.checked)} />
            Retry failed tasks by default
          </label>
          {retryOn && (
            <div className="grid grid-cols-3 gap-2">
              <Field id="job-retry-max" label="Max retries">
                {(p) => <input {...p} type="number" min={1} max={10} className={inputClass} value={retry.maxRetries} onChange={(e) => setRetry({ ...retry, maxRetries: Math.min(10, Math.max(1, Math.trunc(Number(e.target.value)) || 1)) })} />}
              </Field>
              <Field id="job-retry-delay" label="Delay (s)">
                {(p) => <input {...p} type="number" min={0} className={inputClass} value={retry.delaySeconds} onChange={(e) => setRetry({ ...retry, delaySeconds: Math.max(0, Number(e.target.value) || 0) })} />}
              </Field>
              <Field id="job-retry-backoff" label="Backoff">
                {(p) => (
                  <select {...p} className={inputClass} value={retry.backoff} onChange={(e) => setRetry({ ...retry, backoff: e.target.value as Backoff })}>
                    <option value="fixed">Fixed</option>
                    <option value="linear">Linear</option>
                    <option value="exponential">Exponential</option>
                  </select>
                )}
              </Field>
            </div>
          )}
          <p className="text-xs text-slate-500 dark:text-slate-400">A retry override set on an individual task always wins over this default.</p>
        </section>

        <Field
          id="job-ceiling"
          label="Role ceiling for platform access"
          help="Tasks that opt in to platform access get a token for this run capped at this role — and never above what the job's owner (whoever created it) currently holds. There is no owner ceiling: nothing a pipeline does needs workspace administration."
        >
          {(p) => (
            <select {...p} className={inputClass} value={ceiling} onChange={(e) => setCeiling(e.target.value as "viewer" | "editor")}>
              <option value="editor">Editor — can read and write storage and the catalog</option>
              <option value="viewer">Viewer — read only</option>
            </select>
          )}
        </Field>
        {initial && !initial.hasOwner && (
          <Banner tone="warn">
            This job predates workload identity and has no owner, so its scheduled runs cannot get platform access. Saving it makes you its owner.
          </Banner>
        )}

        <label className="flex items-center gap-2 text-sm text-slate-700 dark:text-slate-300">
          <input type="checkbox" checked={concurrent} onChange={(e) => setConcurrent(e.target.checked)} />
          Allow overlapping runs (otherwise a new run is refused while one is still active)
        </label>
      </fieldset>

      {error && !error.field && <Banner tone="error">{error.message}</Banner>}
      {error?.field && !["name", "schedule", "pipelineVersion"].includes(error.field) && <Banner tone="error">{error.message}</Banner>}
      {saved && <Banner tone="success">Job saved.</Banner>}
      {!readOnly && (
        <div>
          <Button type="submit" variant="primary" disabled={busy || name.trim() === "" || pid === ""}>
            {busy ? "Saving…" : initial ? "Save changes" : "Create job"}
          </Button>
        </div>
      )}
    </form>
  );
}
