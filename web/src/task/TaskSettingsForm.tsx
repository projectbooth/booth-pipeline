import { useEffect, useState } from "react";
import { ApiError, api, type ApiContext } from "../api/client";
import { errorMessage, useLoad } from "../hooks";
import type { TaskConfig } from "../types";
import { Banner, Button, inputClass } from "../ui/primitives";
import { TaskConfigForm } from "./TaskConfigForm";

// Edits ONE task's own configuration, with the two saves ADR 0073 defines — shared by the DAG
// builder's node panel (docs/decisions/0016 §4 S2a (c)) and the task page's Edit mode (S5d).
//   Save                  → PUT /tasks/:id/draft: in place, no version. Everything following
//                           "latest" picks it up immediately; pinned references never change.
//   Save as new version…  → POST /tasks/:id/versions: an immutable, numbered, pinnable snapshot.

export const DEFAULT_CONFIG: TaskConfig = {
  code: { type: "catalog", entryId: "", version: "latest" },
  runner: "base",
  retry: null,
  params: {},
  timeoutSeconds: 3600,
  platformAccess: false,
};

export function TaskSettingsForm({
  api: ctx,
  taskId,
  initial,
  readOnly,
  onSaved,
  onDirtyChange,
  onCancel,
}: {
  api: ApiContext;
  taskId: string;
  /** Where the form starts: the task's draft, else its latest version, else DEFAULT_CONFIG. */
  initial: TaskConfig;
  readOnly: boolean;
  onSaved: (what: { kind: "draft" } | { kind: "version"; version: number }) => void;
  onDirtyChange?: (dirty: boolean) => void;
  onCancel?: () => void;
}) {
  const runners = useLoad(() => api.runners(ctx), [ctx]);
  const [config, setConfig] = useState(initial);
  const [baseline, setBaseline] = useState(() => JSON.stringify(initial));
  const [saving, setSaving] = useState<"draft" | "version" | null>(null);
  const [error, setError] = useState<{ message: string; field: string | null } | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [notesOpen, setNotesOpen] = useState(false);
  const [notes, setNotes] = useState("");

  const dirty = JSON.stringify(config) !== baseline;
  useEffect(() => onDirtyChange?.(dirty), [dirty, onDirtyChange]);
  useEffect(() => () => onDirtyChange?.(false), [onDirtyChange]);

  function edit(next: TaskConfig) {
    setConfig(next);
    setError(null);
    setNotice(null);
  }

  function failed(err: unknown) {
    setError({ message: errorMessage(err), field: err instanceof ApiError ? (err.field ?? null) : null });
  }

  async function saveDraft() {
    setSaving("draft");
    setError(null);
    try {
      const d = await api.saveTaskDraft(ctx, taskId, config);
      setConfig(d.config);
      setBaseline(JSON.stringify(d.config));
      setNotice('Saved. Everything following "latest" uses this now; pinned references are unaffected.');
      onSaved({ kind: "draft" });
    } catch (err) {
      failed(err);
    } finally {
      setSaving(null);
    }
  }

  async function saveVersion() {
    setSaving("version");
    setError(null);
    try {
      const saved = await api.saveTaskVersion(ctx, taskId, config, notes.trim());
      setConfig(saved.config);
      setBaseline(JSON.stringify(saved.config));
      setNotesOpen(false);
      setNotes("");
      setNotice(`Saved as task v${saved.version}. It can now be pinned; "latest" references use it too.`);
      onSaved({ kind: "version", version: saved.version });
    } catch (err) {
      failed(err);
    } finally {
      setSaving(null);
    }
  }

  return (
    <div className="flex flex-col gap-4">
      {runners.state.status === "error" && <Banner tone="warn">Runners could not be listed ({runners.state.error}).</Banner>}
      <TaskConfigForm
        config={config}
        runners={runners.state.status === "ready" ? runners.state.data : []}
        api={ctx}
        readOnly={readOnly}
        error={error ?? undefined}
        onChange={edit}
      />
      {notice && <Banner tone="success">{notice}</Banner>}
      {!readOnly && (
        <div className="flex flex-col gap-2">
          <div className="flex flex-wrap items-center gap-2">
            {dirty && <span className="text-xs font-medium text-amber-700 dark:text-amber-400">Unsaved task changes</span>}
            <span className="flex-1" />
            {onCancel && (
              <Button onClick={onCancel} disabled={saving !== null}>
                Cancel
              </Button>
            )}
            <Button onClick={saveDraft} disabled={saving !== null || !dirty}>
              {saving === "draft" ? "Saving…" : "Save"}
            </Button>
            <Button variant="primary" onClick={() => setNotesOpen(true)} disabled={saving !== null || notesOpen}>
              Save as new version…
            </Button>
          </div>
          {notesOpen && (
            <div role="group" aria-label="New task version" className="flex flex-wrap items-center gap-2 rounded-md border border-slate-200 bg-slate-50 p-2 dark:border-slate-700 dark:bg-slate-800/50">
              <input
                aria-label="Task version note"
                autoFocus
                className={`${inputClass} min-w-48 flex-1`}
                placeholder="What changed? (optional)"
                maxLength={4000}
                value={notes}
                onChange={(e) => setNotes(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && void saveVersion()}
              />
              <Button size="sm" onClick={() => setNotesOpen(false)} disabled={saving !== null}>
                Cancel
              </Button>
              <Button size="sm" variant="primary" onClick={saveVersion} disabled={saving !== null}>
                {saving === "version" ? "Saving…" : "Save version"}
              </Button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
