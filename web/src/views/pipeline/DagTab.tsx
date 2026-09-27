import { useMemo, useState } from "react";
import type { ViewCtx } from "../../context";
import { DagCanvas } from "../../dag/DagCanvas";
import { NodeDetails } from "../../dag/NodeDetails";
import { NodeEditor } from "../../dag/NodeEditor";
import type { Resolved } from "../../dag/resolve";
import { formatRelative, formatStamp } from "../../format";
import { TaskPicker } from "../../task/TaskPicker";
import { Badge, Banner, Button, InlineConfirm, Link, inputClass, inputInlineClass, linkClass } from "../../ui/primitives";
import type { LatestRun, PipelineData } from "./data";
import type { Editor } from "./useEditor";

// S2a — the DAG tab (docs/decisions/0016 §4): the builder. Always editable for writers (ruled Q2:
// no view/edit toggle); read-only, with the same canvas, for viewers.
//
// Layout, top to bottom, all in normal block flow: toolbar → banners → the fixed-height canvas →
// ONE panel (hint | add-a-task picker | selected node). Nothing ever opens beside or over the
// canvas, so nothing that opens can change its size (0016 §6 — "all of the menu popups should be
// under the pipeline DAG view").

export function DagTab({
  v,
  d,
  editor: ed,
  resolved,
  latestRun,
  onTaskSaved,
  onTaskDirty,
}: {
  v: ViewCtx;
  d: PipelineData;
  editor: Editor;
  resolved: Resolved;
  latestRun: LatestRun;
  onTaskSaved: () => void;
  onTaskDirty: (dirty: boolean) => void;
}) {
  const p = d.pipeline;
  const canEdit = v.canWrite;
  const [versionNoteOpen, setVersionNoteOpen] = useState(false);
  const [notes, setNotes] = useState("");
  const [confirmDiscard, setConfirmDiscard] = useState(false);

  const refs = ed.refs;
  const statuses = useMemo(() => latestRun.statusesFor(refs), [latestRun, refs]);
  const run = latestRun.run;
  const selectedRef = refs.find((r) => r.key === ed.selected) ?? null;
  const busy = ed.saving !== null;

  async function saveVersion() {
    if (await ed.saveVersion(notes)) {
      setVersionNoteOpen(false);
      setNotes("");
    }
  }

  return (
    <div className="flex flex-col gap-3">
      {/* Row 1: which version is shown, and whose statuses colour the nodes. */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <label className="flex items-center gap-2 text-sm text-slate-600 dark:text-slate-300">
          Showing
          <select
            aria-label="Pipeline version shown"
            className={inputInlineClass}
            value={d.viewed ? String(d.viewed.version) : ""}
            onChange={(e) => v.go(e.target.value === "" ? { name: "pipeline", id: p.id } : { name: "pipeline", id: p.id, version: Number(e.target.value) })}
          >
            <option value="">{d.draftDiffers || !d.latest ? "Current draft" : `Current (v${d.latest.version})`}</option>
            {d.versions.map((ver) => (
              <option key={ver.version} value={ver.version}>
                v{ver.version} — {formatStamp(ver.createdAt)} — {ver.taskCount} task{ver.taskCount === 1 ? "" : "s"}
                {ver.notes ? ` — ${ver.notes}` : ""}
              </option>
            ))}
          </select>
        </label>
        {run && (
          <p className="text-xs text-slate-500 dark:text-slate-400">
            Status from{" "}
            <Link href={v.href({ name: "run", id: run.id })} onNavigate={v.goPath} className={`${linkClass} font-mono`}>
              run {run.id.slice(0, 8)}
            </Link>{" "}
            (v{run.pipelineVersion}, {formatRelative(run.startedAt ?? run.createdAt)})
          </p>
        )}
      </div>

      {/* Row 2: the builder toolbar (writers only). */}
      {canEdit && (
        <div role="toolbar" aria-label="Pipeline builder" className="flex flex-wrap items-center gap-2">
          <Button variant="primary" onClick={ed.startAdding} disabled={ed.adding}>
            + Add task
          </Button>
          <Button onClick={ed.tidy} disabled={refs.length < 2}>
            Tidy layout
          </Button>
          {ed.dirty && <Badge tone="amber">Unsaved changes</Badge>}
          <span className="flex-1" />
          <Button onClick={() => setConfirmDiscard(true)} disabled={!ed.dirty || busy}>
            Discard
          </Button>
          <Button onClick={ed.saveDraft} disabled={!ed.dirty || busy}>
            {ed.saving === "draft" ? "Saving…" : "Save"}
          </Button>
          <Button variant="primary" onClick={() => setVersionNoteOpen(true)} disabled={busy || versionNoteOpen || refs.length === 0 || (!ed.dirty && !d.draftDiffers && !d.viewed && p.latestVersion > 0)}>
            Save as new version…
          </Button>
        </div>
      )}

      {confirmDiscard && (
        <InlineConfirm
          message="Discard all unsaved changes to this DAG?"
          confirmLabel="Discard changes"
          onConfirm={() => {
            ed.discard();
            setConfirmDiscard(false);
          }}
          onCancel={() => setConfirmDiscard(false)}
        />
      )}
      {versionNoteOpen && (
        <div role="group" aria-label="New pipeline version" className="flex flex-wrap items-center gap-2 rounded-md border border-slate-300 bg-slate-50 p-2 dark:border-slate-600 dark:bg-slate-900">
          <span className="text-sm text-slate-700 dark:text-slate-200">Save as v{p.latestVersion + 1}:</span>
          <input
            aria-label="Version note"
            autoFocus
            className={`${inputClass} min-w-48 flex-1`}
            placeholder="What changed? (optional)"
            maxLength={4000}
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && void saveVersion()}
          />
          <Button size="sm" onClick={() => setVersionNoteOpen(false)} disabled={busy}>
            Cancel
          </Button>
          <Button size="sm" variant="primary" onClick={saveVersion} disabled={busy}>
            {ed.saving === "version" ? "Saving…" : "Save version"}
          </Button>
        </div>
      )}

      {d.viewed && d.latest && d.viewed.version !== d.latest.version && (
        <Banner tone="info">
          Viewing v{d.viewed.version} of {d.latest.version} (saved {formatStamp(d.viewed.createdAt)}).{" "}
          {canEdit ? `Saving from here creates v${d.latest.version + 1} from this one; ` : ""}nothing pinned to a version changes.
        </Banner>
      )}
      {ed.notice && <Banner tone={ed.notice.tone}>{ed.notice.text}</Banner>}
      {ed.saveError && <Banner tone="error">{ed.saveError.message}</Banner>}
      {!ed.saveError && ed.live && !ed.live.valid && <Banner tone="warn">Not ready to save as a version: {ed.live.error}</Banner>}
      {!canEdit && <Banner tone="info">Your role in this workspace is read-only, so the builder is view-only.</Banner>}

      <DagCanvas
        label="Pipeline DAG"
        refs={refs}
        resolved={resolved}
        selected={ed.selected}
        onSelect={ed.select}
        onChange={canEdit ? ed.change : undefined}
        statuses={statuses}
        errors={ed.nodeErrors}
        theme={v.theme}
        empty={
          <div className="pointer-events-auto max-w-sm rounded-lg border border-dashed border-slate-300 bg-white p-6 text-center dark:border-slate-600 dark:bg-slate-900">
            <p className="text-sm font-medium text-slate-800 dark:text-slate-100">This pipeline has no tasks yet</p>
            {canEdit && (
              <div className="mt-3">
                <Button variant="primary" onClick={ed.startAdding}>
                  + Add task
                </Button>
              </div>
            )}
          </div>
        }
      />

      {/* The one panel, below the canvas. */}
      {canEdit && ed.adding ? (
        <TaskPicker api={v.api} title="Add a task" onPick={ed.addTask} onCancel={ed.stopAdding} />
      ) : selectedRef ? (
        canEdit ? (
          <NodeEditor
            key={selectedRef.key}
            v={v}
            taskRef={selectedRef}
            refs={refs}
            node={resolved[selectedRef.key]}
            lastRun={run}
            runCounts={selectedRef.key in statuses}
            error={ed.problemFor(selectedRef.key)}
            onChangeRef={(next) => ed.updateRef(selectedRef.key, next)}
            onRename={ed.rename}
            onSetDeps={ed.setDeps}
            onRemove={ed.remove}
            onTaskSaved={onTaskSaved}
            onTaskDirty={onTaskDirty}
          />
        ) : (
          <NodeDetails v={v} taskRef={selectedRef} node={resolved[selectedRef.key]} lastRun={run} runCounts={selectedRef.key in statuses} />
        )
      ) : (
        refs.length > 0 && (
          <p className="text-sm text-slate-500 dark:text-slate-400">
            Select a task to see {canEdit ? "and edit " : ""}its details here.
            {canEdit && " Drag from a task's right-hand handle to another task to make it depend on this one; Backspace removes the selected task."}
          </p>
        )
      )}
    </div>
  );
}
