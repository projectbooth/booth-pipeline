import { useEffect, useMemo, useState } from "react";
import { api, type ApiContext } from "../api/client";
import { errorMessage } from "../hooks";
import type { TaskConfig, TaskEntity, TaskRef } from "../types";

// What each DAG node actually points at, for display: the referenced task's name, and the config
// the node would run. Mirrors the backend's own `resolve_task_ref` (ADR 0073): a pinned version
// resolves to exactly that immutable version; "latest" resolves to the task's draft if it has one,
// else its latest saved version. The server stays the authority at run time — this only labels
// nodes and fills the read-only panels, and never blocks anything when a lookup fails.

export type ResolvedFrom = { kind: "version"; version: number } | { kind: "draft" } | { kind: "none" };

export interface ResolvedNode {
  task: TaskEntity | null;
  config: TaskConfig | null;
  from: ResolvedFrom;
  /** The (task, version reference) this was resolved for — lets a caller tell a fresh result from
   *  one still describing a node's previous target while the new lookup is in flight. */
  forTask: string;
  forVersion: TaskRef["taskVersion"];
  error?: string;
}

export type Resolved = Record<string, ResolvedNode>; // keyed by node key

async function resolveOne(ctx: ApiContext, ref: TaskRef, task: TaskEntity): Promise<ResolvedNode> {
  const f = { forTask: ref.taskId, forVersion: ref.taskVersion };
  if (ref.taskVersion !== "latest") {
    const v = await api.getTaskVersion(ctx, ref.taskId, ref.taskVersion);
    return { ...f, task, config: v.config, from: { kind: "version", version: v.version } };
  }
  if (task.hasDraft) {
    const d = await api.getTaskDraft(ctx, ref.taskId);
    return { ...f, task, config: d.config, from: { kind: "draft" } };
  }
  if (task.latestVersion > 0) {
    const v = await api.getTaskVersion(ctx, ref.taskId, task.latestVersion);
    return { ...f, task, config: v.config, from: { kind: "version", version: v.version } };
  }
  return { ...f, task, config: null, from: { kind: "none" } };
}

export async function resolveRefs(ctx: ApiContext, refs: TaskRef[]): Promise<Resolved> {
  const ids = [...new Set(refs.map((r) => r.taskId))];
  const tasks = new Map<string, TaskEntity | Error>();
  await Promise.all(
    ids.map((id) =>
      api.getTask(ctx, id).then(
        (t) => void tasks.set(id, t),
        (e: unknown) => void tasks.set(id, e instanceof Error ? e : new Error(String(e))),
      ),
    ),
  );
  const out: Resolved = {};
  await Promise.all(
    refs.map(async (r) => {
      const t = tasks.get(r.taskId);
      if (!t || t instanceof Error) {
        out[r.key] = { forTask: r.taskId, forVersion: r.taskVersion, task: null, config: null, from: { kind: "none" }, error: t ? t.message : "task not found" };
        return;
      }
      try {
        out[r.key] = await resolveOne(ctx, r, t);
      } catch (err) {
        out[r.key] = { forTask: r.taskId, forVersion: r.taskVersion, task: t, config: null, from: { kind: "none" }, error: errorMessage(err) };
      }
    }),
  );
  return out;
}

/** Resolves `refs` whenever the set of (key, task, version) triples changes — NOT on every
 *  position change, so dragging a node never refetches anything. Keeps showing the previous
 *  result while a new one loads, so node labels never flicker to blank. */
export function useResolved(ctx: ApiContext, refs: TaskRef[], refreshToken = 0): { resolved: Resolved; loading: boolean } {
  const signature = useMemo(() => JSON.stringify(refs.map((r) => [r.key, r.taskId, r.taskVersion])), [refs]);
  const [resolved, setResolved] = useState<Resolved>({});
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let alive = true;
    setLoading(true);
    const current = (JSON.parse(signature) as [string, string, TaskRef["taskVersion"]][]).map(
      ([key, taskId, taskVersion]): TaskRef => ({ key, taskId, taskVersion, dependsOn: [], position: { x: 0, y: 0 } }),
    );
    resolveRefs(ctx, current).then((r) => {
      if (!alive) return;
      setResolved(r);
      setLoading(false);
    });
    return () => {
      alive = false;
    };
  }, [ctx, signature, refreshToken]);
  return { resolved, loading };
}

/** "PY" / "SQL" — the badge on a node. From the code snapshot's language, which the server fills
 *  in at save time for catalog and storage code; old inline code is always Python. */
export function languageOf(config: TaskConfig | null): string | null {
  if (!config) return null;
  const lang = config.code.type === "inline" ? "python" : config.code.language;
  if (!lang) return null;
  return lang === "python" ? "PY" : lang.toUpperCase();
}

/** Human description of where a node's code comes from. */
export function codeRefLabel(config: TaskConfig): string {
  const c = config.code;
  if (c.type === "inline") return "inline (legacy)";
  if (c.type === "catalog") return `catalog: ${c.name ?? c.entryId} @ ${c.version}`;
  return `storage: ${c.backendId}:${c.path}`;
}

export function versionRefLabel(ref: Pick<TaskRef, "taskVersion">): string {
  return ref.taskVersion === "latest" ? "latest" : `v${ref.taskVersion}`;
}

export function resolvedFromLabel(from: ResolvedFrom): string {
  return from.kind === "draft" ? "draft" : from.kind === "version" ? `v${from.version}` : "not configured";
}
