import { useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api/client";
import { DagCanvas } from "../components/DagCanvas";
import { PipelineScheduleForm } from "../components/PipelineScheduleForm";
import { ReferencePanel } from "../components/ReferencePanel";
import { TaskPicker } from "../components/TaskPicker";
import { Banner, Button, Chip, Loaded, PageHeader, inputClass } from "../components/ui";
import type { ViewCtx } from "../context";
import { formatTime } from "../format";
import { autoLayout, hasOverlappingPositions, newRef, parseTaskField, removeRef, renameKey } from "../graph";
import { errorMessage, useDebounced, useLoad } from "../hooks";
import type { Pipeline, PipelineDraft, PipelineVersion, RunnerInfo, TaskEntity, TaskRef, ValidateResult } from "../types";

// The graphical DAG builder: draw a Pipeline as references to standalone Tasks (ADR 0071), wire
// their dependencies, and save. Every save is a NEW immutable version — the canvas edits a draft,
// and nothing changes for anyone running this pipeline until it is saved and, if pinned, re-pinned.

export function PipelineEditor({ v, pipelineId, version }: { v: ViewCtx; pipelineId: string; version?: number }) {
  // Lives here, not in Editor: a save reloads the pipeline, the editor is re-keyed to the new
  // latest version and remounts, and its own state (this banner included) would be lost.
  const [savedAs, setSavedAs] = useState<number | null>(null);
  const [tab, setTab] = useState<"builder" | "schedule">("builder");
  const load = useLoad(
    async () => {
      const [pipeline, versions, runners] = await Promise.all([
        api.getPipeline(v.api, pipelineId),
        api.listVersions(v.api, pipelineId),
        api.runners(v.api),
      ]);
      // A pipeline that has never been saved has no version to load: it starts as a blank canvas.
      const current: PipelineVersion | null =
        pipeline.latestVersion === 0 ? null : await api.getVersion(v.api, pipelineId, version ?? "latest");
      // Viewing an explicit older version (a `version` route param) never loads the draft — that
      // view is exactly what was saved, on purpose. Viewing "current" prefers the draft when there
      // is one (ADR 0073 — always at least as fresh as `current` above, which stays around for the
      // version-switcher's selected value, the "viewing an old version" banner, and Export).
      const draft: PipelineDraft | null = version === undefined && pipeline.hasDraft ? await api.getPipelineDraft(v.api, pipelineId) : null;
      return { pipeline, versions: versions.items, runners, current, draft };
    },
    [v.api, pipelineId, version],
  );
  return (
    <Loaded state={load.state} retry={load.reload}>
      {(d) => (
        <div className="flex flex-col gap-3">
          <PageHeader
            title={d.pipeline.name}
            subtitle={d.pipeline.description || undefined}
            actions={
              <nav aria-label="Pipeline views" className="flex gap-1">
                {([
                  { key: "builder", label: "Builder" },
                  { key: "schedule", label: "Schedule" },
                ] as const).map((t) => (
                  <button
                    key={t.key}
                    type="button"
                    aria-current={tab === t.key ? "page" : undefined}
                    onClick={() => setTab(t.key)}
                    className={`rounded-md px-3 py-1.5 text-sm font-medium ${
                      tab === t.key ? "bg-indigo-600 text-white" : "text-slate-700 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800"
                    }`}
                  >
                    {t.label}
                  </button>
                ))}
              </nav>
            }
          />
          {tab === "builder" ? (
            <Editor
              key={`${pipelineId}:${d.current?.version ?? 0}:${d.draft?.updatedAt ?? ""}`}
              v={v}
              pipeline={d.pipeline}
              versions={d.versions}
              runners={d.runners}
              current={d.current}
              draft={d.draft}
              reload={load.reload}
              savedAs={savedAs}
              setSavedAs={setSavedAs}
            />
          ) : (
            <PipelineScheduleForm v={v} pipeline={d.pipeline} versions={d.versions} onSaved={() => load.reload()} />
          )}
        </div>
      )}
    </Loaded>
  );
}

interface EditorProps {
  v: ViewCtx;
  pipeline: Pipeline;
  versions: { version: number; notes: string; createdBy: string; createdAt: string; taskCount: number }[];
  runners: RunnerInfo[];
  current: PipelineVersion | null;
  draft: PipelineDraft | null;
  reload: () => void;
  savedAs: number | null;
  setSavedAs: (n: number | null) => void;
}

const spec = (tasks: TaskRef[]) => ({ tasks });

function Editor({ v, pipeline, versions, runners, current, draft, reload, savedAs, setSavedAs }: EditorProps) {
  // The draft if there is one (ADR 0073 — always at least as fresh as `current`, since both plain
  // Save and "Save as new version" write it), else the loaded version, else a blank canvas. If two
  // or more references share the exact same position (the shared {0,0} default for anything built
  // directly against the API, never dragged in the builder — see graph.ts's
  // hasOverlappingPositions), lay it out once on open rather than showing what looks like an empty
  // or broken canvas. Baseline is set from the SAME laid-out array, so this never shows as an
  // unsaved change — only actually editing does. `Editor` remounts (its key includes the version
  // and the draft's own timestamp) whenever either changes, so computing this once at mount —
  // rather than depending on `current`/`draft` — is deliberate, not an oversight.
  const initialRefs = useMemo(() => {
    const loaded = draft ? draft.spec.tasks : (current?.spec.tasks ?? []);
    return hasOverlappingPositions(loaded) ? autoLayout(loaded) : loaded;
  }, []);
  const [refs, setRefs] = useState<TaskRef[]>(initialRefs);
  const [baseline, setBaseline] = useState(() => JSON.stringify(initialRefs));
  // Whether the loaded draft actually holds something different from the latest saved version —
  // not just "a draft row exists", since a version-save also writes the draft to match it exactly
  // (see `save` below), so most of the time they agree and calling that out would be noise.
  const [draftDiffers] = useState(() => draft !== null && JSON.stringify(draft.spec.tasks) !== JSON.stringify(current?.spec.tasks ?? []));
  const [selected, setSelected] = useState<string | null>(null);
  const [taskNames, setTaskNames] = useState<Record<string, string>>({});
  const [notes, setNotes] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<{ message: string; field?: string } | null>(null);
  const [savedDraft, setSavedDraft] = useState(false);
  const [live, setLive] = useState<ValidateResult | null>(null);
  const [addingTask, setAddingTask] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);

  const dirty = JSON.stringify(refs) !== baseline;
  const readOnly = !v.canWrite;
  const latest = pipeline.latestVersion;
  const viewingOld = current !== null && current.version !== latest;

  // Warn before a reload/close throws away drawn work.
  useEffect(() => {
    if (!dirty) return;
    const h = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener("beforeunload", h);
    return () => window.removeEventListener("beforeunload", h);
  }, [dirty]);

  // Live validation, debounced, by the SERVER — the single source of truth for the rules, so the
  // canvas can never disagree with what save will accept.
  const debounced = useDebounced(refs, 500);
  useEffect(() => {
    if (debounced.length === 0) return setLive(null);
    let alive = true;
    api.validate(v.api, spec(debounced)).then(
      (r) => alive && setLive(r),
      () => alive && setLive(null), // the check itself failing is not the user's problem; save reports for real
    );
    return () => {
      alive = false;
    };
  }, [debounced, v.api]);

  const problem = saveError ?? (live && !live.valid ? { message: live.error ?? "invalid", field: live.field ?? undefined } : null);
  const parsedField = useMemo(() => parseTaskField(problem?.field), [problem]);
  const refErrors = useMemo(() => {
    const r = parsedField && refs[parsedField.index];
    return r ? { [r.key]: problem?.message ?? "" } : {};
  }, [parsedField, refs, problem]);

  const change = useCallback((next: TaskRef[]) => {
    setRefs(next);
    setSaveError(null);
    setSavedAs(null);
    setSavedDraft(false);
  }, [setSavedAs]);

  function openTaskPicker() {
    setSelected(null);
    setAddingTask(true);
  }

  function pickTaskRef(task: TaskEntity) {
    setTaskNames((n) => (n[task.id] === task.name ? n : { ...n, [task.id]: task.name }));
    const r = newRef(task.id, refs);
    change([...refs, r]);
    setAddingTask(false);
    setSelected(r.key);
  }

  async function save() {
    setSaving(true);
    setSaveError(null);
    try {
      const saved = await api.saveVersion(v.api, pipeline.id, spec(refs), notes.trim());
      setSavedAs(saved.version);
      setNotes("");
      setRefs(saved.spec.tasks);
      setBaseline(JSON.stringify(saved.spec.tasks));
      v.goPath(v.href({ name: "pipeline", id: pipeline.id }));
      reload();
    } catch (err) {
      setSaveError({ message: errorMessage(err), field: err instanceof ApiError ? err.field : undefined });
      const parsed = err instanceof ApiError ? parseTaskField(err.field) : null;
      if (parsed && refs[parsed.index]) setSelected(refs[parsed.index].key);
    } finally {
      setSaving(false);
    }
  }

  // Plain "Save" (ADR 0073): writes the draft in place, no version created, no navigation or
  // remount — unlike `save` above, this stays on the same screen so it is as low-friction as the
  // user asked for ("I don't want to save a new version every time I make a change").
  async function saveDraft() {
    setSaving(true);
    setSaveError(null);
    try {
      const d = await api.savePipelineDraft(v.api, pipeline.id, spec(refs));
      setRefs(d.spec.tasks);
      setBaseline(JSON.stringify(d.spec.tasks));
      setSavedDraft(true);
    } catch (err) {
      setSaveError({ message: errorMessage(err), field: err instanceof ApiError ? err.field : undefined });
      const parsed = err instanceof ApiError ? parseTaskField(err.field) : null;
      if (parsed && refs[parsed.index]) setSelected(refs[parsed.index].key);
    } finally {
      setSaving(false);
    }
  }

  async function exportYaml() {
    if (!current) return;
    setExporting(true);
    setExportError(null);
    try {
      const text = await api.exportVersion(v.api, pipeline.id, current.version);
      const url = URL.createObjectURL(new Blob([text], { type: "application/yaml" }));
      const a = document.createElement("a");
      a.href = url;
      a.download = `${pipeline.name}-v${current.version}.yaml`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      setExportError(errorMessage(err));
    } finally {
      setExporting(false);
    }
  }

  const selectedRef = refs.find((r) => r.key === selected) ?? null;
  // The bit of `problem` that is about the SELECTED node specifically, with the field path kept
  // relative to it (the "tasks[N]." prefix stripped) — ReferencePanel maps `field` onto its own
  // inputs the same way its `keyError` already does, instead of only ever showing the generic message.
  const selectedRefError =
    selectedRef && parsedField && refs[parsedField.index]?.key === selectedRef.key && problem
      ? { message: problem.message, field: parsedField.rest }
      : undefined;

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2" role="toolbar" aria-label="Pipeline builder">
        {!readOnly && (
          <>
            <Button variant="primary" onClick={openTaskPicker}>
              + Add task
            </Button>
            <Button onClick={() => change(autoLayout(refs))} disabled={refs.length === 0}>
              Tidy layout
            </Button>
          </>
        )}
        {dirty && <Chip tone="amber">Unsaved changes</Chip>}
        {!dirty && (draftDiffers || savedDraft) && <Chip tone="sky">Draft ahead of the latest saved version</Chip>}
        <span className="flex-1" />
        <label htmlFor="version-select" className="text-xs text-slate-500 dark:text-slate-400">
          Version
        </label>
        <select
          id="version-select"
          className={`${inputClass} w-auto`}
          value={current?.version ?? ""}
          disabled={versions.length === 0}
          onChange={(e) => {
            const n = Number(e.target.value);
            v.goPath(v.href(n === latest ? { name: "pipeline", id: pipeline.id } : { name: "pipeline", id: pipeline.id, version: n }));
          }}
        >
          {versions.length === 0 && <option value="">not saved yet</option>}
          {versions.map((ver) => (
            <option key={ver.version} value={ver.version}>
              v{ver.version}
              {ver.version === latest ? " (latest)" : ""} — {formatTime(ver.createdAt)} — {ver.taskCount} tasks{ver.notes ? ` — ${ver.notes}` : ""}
            </option>
          ))}
        </select>
        <Button onClick={exportYaml} disabled={!current || exporting}>
          {exporting ? "Exporting…" : "Export as YAML"}
        </Button>
        {!readOnly && (
          <>
            <Button onClick={saveDraft} disabled={saving || refs.length === 0 || !dirty}>
              {saving ? "Saving…" : "Save"}
            </Button>
            <input
              aria-label="Version note"
              className={`${inputClass} w-56`}
              placeholder="What changed? (optional)"
              value={notes}
              maxLength={200}
              onChange={(e) => setNotes(e.target.value)}
            />
            <Button variant="primary" onClick={save} disabled={saving || refs.length === 0 || (!dirty && latest > 0)}>
              {saving ? "Saving…" : "Save as new version"}
            </Button>
          </>
        )}
      </div>

      {exportError && <Banner tone="error">{exportError}</Banner>}
      {viewingOld && (
        <Banner tone="info">
          You are viewing version {current?.version} of {latest}. Saving creates version {latest + 1} from this one; nothing pinned to an older version changes.
        </Banner>
      )}
      {savedDraft && (
        <Banner tone="success">Saved. "Always follow latest" references pick this up immediately; nothing pinned to a specific version is affected.</Banner>
      )}
      {savedAs !== null && <Banner tone="success">Saved as version {savedAs}. Pipelines set to follow the latest version will use it from their next run.</Banner>}
      {saveError && <Banner tone="error">{saveError.message}</Banner>}
      {!saveError && live && !live.valid && <Banner tone="warn">Not ready to save: {live.error}</Banner>}
      {readOnly && <Banner tone="info">Your role in this workspace is read-only, so the builder is view-only.</Banner>}

      <div className="flex min-h-[32rem] flex-col gap-3 lg:flex-row">
        <div className="relative h-[32rem] min-w-0 shrink-0 overflow-hidden rounded-md border border-slate-300 bg-slate-50 dark:border-slate-700 dark:bg-slate-950 lg:flex-1">
          <DagCanvas
            refs={refs}
            selected={selected}
            onSelect={(key) => {
              setSelected(key);
              if (key) setAddingTask(false);
            }}
            onChange={readOnly ? undefined : change}
            taskNames={taskNames}
            errors={refErrors}
            theme={v.theme}
          />
          {refs.length === 0 && (
            <div className="pointer-events-none absolute inset-0 flex items-center justify-center p-6">
              <div className="pointer-events-auto max-w-sm rounded-md border border-dashed border-slate-300 bg-white p-6 text-center dark:border-slate-600 dark:bg-slate-900">
                <p className="text-sm font-medium text-slate-800 dark:text-slate-100">This pipeline is empty</p>
                <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">Add a task to get started, or drag one in from the canvas handles once you have more than one.</p>
                {!readOnly && (
                  <div className="mt-3">
                    <Button variant="primary" onClick={openTaskPicker}>
                      + Add task
                    </Button>
                  </div>
                )}
              </div>
            </div>
          )}
        </div>

        <aside aria-label="Task settings" className="max-h-[32rem] w-full overflow-auto rounded-md border border-slate-200 p-3 lg:w-[26rem] dark:border-slate-700">
          {selectedRef ? (
            <ReferencePanel
              taskRef={selectedRef}
              refs={refs}
              runners={runners}
              api={v.api}
              v={v}
              readOnly={readOnly}
              error={selectedRefError}
              onChangeRef={(r) => change(refs.map((x) => (x.key === selectedRef.key ? r : x)))}
              onTaskNameKnown={(taskId, name) => setTaskNames((n) => (n[taskId] === name ? n : { ...n, [taskId]: name }))}
              onRename={(from, to) => {
                change(renameKey(refs, from, to));
                setSelected(to);
              }}
              onSetDependencies={(key, deps) => change(refs.map((r) => (r.key === key ? { ...r, dependsOn: deps } : r)))}
              onRemove={(key) => {
                change(removeRef(refs, key));
                setSelected(null);
              }}
            />
          ) : addingTask ? (
            <div className="flex flex-col gap-2">
              <h3 className="text-sm font-semibold text-slate-800 dark:text-slate-100">Add a task</h3>
              <TaskPicker api={v.api} onPick={pickTaskRef} onCancel={() => setAddingTask(false)} />
            </div>
          ) : (
            <p className="text-sm text-slate-500 dark:text-slate-400">
              Select a task to configure which version it references, its dependencies, and its own code and settings.
              {!readOnly && " Drag from a task's right-hand handle to another task to make it depend on this one."}
            </p>
          )}
        </aside>
      </div>
    </div>
  );
}
