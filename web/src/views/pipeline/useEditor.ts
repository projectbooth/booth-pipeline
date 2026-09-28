import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, api } from "../../api/client";
import type { ViewCtx } from "../../context";
import { autoLayout, duplicateRef, hasOverlappingPositions, newRef, parseTaskField, removeRef, renameKey } from "../../graph";
import { errorMessage, useDebounced } from "../../hooks";
import type { TaskEntity, TaskRef, ValidateResult } from "../../types";
import { currentSpec, type PipelineData } from "./data";

// The DAG builder's state (docs/decisions/0016 §4 S2a). Owned by the pipeline PAGE rather than the
// DAG tab, so switching to Runs / Schedule / … and back keeps unsaved edits.
//
// Two saves (ADR 0073): plain Save writes the pipeline's mutable draft in place; "Save as new
// version" snapshots an immutable, numbered, pinnable version. Nothing pinned is ever affected by
// either. The SERVER is the only authority on what's valid: edits are checked live against
// POST /pipelines/validate, and a save's own refusal is routed to the node and field it names.

export interface Problem {
  message: string;
  field?: string;
}

export type Notice = { tone: "success" | "info"; text: string } | null;

const specOf = (tasks: TaskRef[]) => ({ tasks });
const layoutIfStacked = (refs: TaskRef[]) => (hasOverlappingPositions(refs) ? autoLayout(refs) : refs);

export function useEditor(v: ViewCtx, d: PipelineData, reload: () => void) {
  const p = d.pipeline;
  const [refs, setRefs] = useState<TaskRef[]>(() => layoutIfStacked(currentSpec(d)));
  const [baseline, setBaseline] = useState(() => JSON.stringify(refs));
  const [selected, setSelected] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [saving, setSaving] = useState<"draft" | "version" | null>(null);
  const [saveError, setSaveError] = useState<Problem | null>(null);
  const [live, setLive] = useState<ValidateResult | null>(null);
  const [notice, setNotice] = useState<Notice>(null);

  const dirty = JSON.stringify(refs) !== baseline;

  // Fresh data from the server (after a save, or a reload) replaces the canvas — but never while
  // the user has unsaved edits of their own.
  const lastData = useRef(d);
  useEffect(() => {
    if (lastData.current === d) return;
    lastData.current = d;
    if (dirty) return;
    const next = layoutIfStacked(currentSpec(d));
    setRefs(next);
    setBaseline(JSON.stringify(next));
  }, [d, dirty]);

  // Live validation, debounced, by the server.
  const debounced = useDebounced(refs, 500);
  useEffect(() => {
    if (debounced.length === 0) return setLive(null);
    let alive = true;
    api.validate(v.api, specOf(debounced)).then(
      (r) => alive && setLive(r),
      () => alive && setLive(null), // the check itself failing isn't the user's problem; save reports for real
    );
    return () => {
      alive = false;
    };
  }, [debounced, v.api]);

  const problem: Problem | null = saveError ?? (live && !live.valid ? { message: live.error ?? "invalid", field: live.field ?? undefined } : null);
  const parsed = useMemo(() => parseTaskField(problem?.field), [problem]);
  const problemKey = parsed ? (refs[parsed.index]?.key ?? null) : null;
  const nodeErrors = useMemo<Record<string, string>>(() => (problemKey && problem ? { [problemKey]: problem.message } : {}), [problemKey, problem]);

  /** Every edit goes through here: it clears stale save feedback. */
  const change = useCallback((next: TaskRef[]) => {
    setRefs(next);
    setSaveError(null);
    setNotice(null);
  }, []);

  function select(key: string | null) {
    setSelected(key);
    if (key) setAdding(false);
  }

  function startAdding() {
    setSelected(null);
    setAdding(true);
  }

  function addTask(task: TaskEntity) {
    const r = newRef(task.id, refs);
    change([...refs, r]);
    setAdding(false);
    setSelected(r.key);
  }

  function failed(err: unknown) {
    const field = err instanceof ApiError ? err.field : undefined;
    setSaveError({ message: errorMessage(err), field });
    const at = parseTaskField(field);
    if (at && refs[at.index]) select(refs[at.index].key);
  }

  /** After saving while looking at an old version, go back to the pipeline's current view — what
   *  was just saved IS the current state now. The guard is cleared first: nothing is unsaved. */
  function afterSave() {
    if (d.viewed) {
      v.setLeaveGuard(null);
      v.go({ name: "pipeline", id: p.id });
    } else {
      reload();
    }
  }

  const runsWhat = p.pinnedVersion ? `pinned v${p.pinnedVersion}` : p.latestVersion > 0 ? `v${p.latestVersion}` : "nothing yet (no saved version)";

  async function saveDraft() {
    setSaving("draft");
    setSaveError(null);
    try {
      const saved = await api.savePipelineDraft(v.api, p.id, specOf(refs));
      setRefs(saved.spec.tasks);
      setBaseline(JSON.stringify(saved.spec.tasks));
      setNotice({ tone: "success", text: `Saved. Runs still use ${runsWhat} until you save a new version; "always follow latest" task references already pick up task saves.` });
      afterSave();
    } catch (err) {
      failed(err);
    } finally {
      setSaving(null);
    }
  }

  async function saveVersion(notes: string): Promise<boolean> {
    setSaving("version");
    setSaveError(null);
    try {
      const saved = await api.saveVersion(v.api, p.id, specOf(refs), notes.trim());
      setRefs(saved.spec.tasks);
      setBaseline(JSON.stringify(saved.spec.tasks));
      setNotice({
        tone: "success",
        text: p.pinnedVersion ? `Saved as v${saved.version}. Runs stay on pinned v${p.pinnedVersion} until the pin is changed.` : `Saved as v${saved.version}. The next run uses it.`,
      });
      afterSave();
      return true;
    } catch (err) {
      failed(err);
      return false;
    } finally {
      setSaving(null);
    }
  }

  function discard() {
    const next = layoutIfStacked(currentSpec(d));
    setRefs(next);
    setBaseline(JSON.stringify(next));
    setSaveError(null);
    setNotice(null);
    setSelected(null);
    setAdding(false);
  }

  return {
    refs,
    change,
    dirty,
    selected,
    select,
    adding,
    startAdding,
    stopAdding: () => setAdding(false),
    addTask,
    tidy: () => change(autoLayout(refs)),
    rename: (from: string, to: string) => {
      change(renameKey(refs, from, to));
      setSelected(to);
    },
    setDeps: (key: string, deps: string[]) => change(refs.map((r) => (r.key === key ? { ...r, dependsOn: deps } : r))),
    updateRef: (key: string, next: TaskRef) => change(refs.map((r) => (r.key === key ? next : r))),
    remove: (key: string) => {
      change(removeRef(refs, key));
      setSelected(null);
    },
    duplicate: (key: string) => {
      const out = duplicateRef(refs, key);
      if (!out.key) return;
      change(out.refs);
      setSelected(out.key);
    },
    saving,
    saveDraft,
    saveVersion,
    discard,
    saveError,
    live,
    problem,
    /** The part of `problem` about ONE node, with its field relative to that node ("tasks[N]." stripped). */
    problemFor: (key: string): { message: string; field: string | null } | undefined =>
      problem && problemKey === key ? { message: problem.message, field: parsed?.rest ?? null } : undefined,
    nodeErrors,
    notice,
  };
}

export type Editor = ReturnType<typeof useEditor>;
