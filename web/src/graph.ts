// Pure graph logic for the builder: converting between a PipelineSpec and what the canvas draws,
// what a new reference looks like, and which connections the canvas should allow. Kept free of
// React and of @xyflow/react so it is trivially unit-testable — the canvas component is thin glue
// over this. The SERVER remains the authority (src/booth_pipeline/model.py validate_structure):
// these rules exist so the canvas never lets a user draw something that could not possibly be
// saved, not to replace the server's check.

import type { TaskRef, TaskStatus } from "./types";

export const NODE_WIDTH = 220;
export const NODE_HEIGHT = 84;
const COL_GAP = 90;
const ROW_GAP = 40;

export interface FlowNode {
  id: string;
  type: "task";
  position: { x: number; y: number };
  data: { ref: TaskRef; taskName?: string; status?: TaskStatus; error?: string };
  selected?: boolean;
}

export interface FlowEdge {
  id: string;
  source: string;
  target: string;
}

export function edgeId(from: string, to: string): string {
  return `${from}->${to}`;
}

export function toFlow(
  refs: TaskRef[],
  opts: { taskNames?: Record<string, string>; statuses?: Record<string, TaskStatus>; errors?: Record<string, string>; selected?: string | null } = {},
): { nodes: FlowNode[]; edges: FlowEdge[] } {
  const nodes: FlowNode[] = refs.map((r) => ({
    id: r.key,
    type: "task",
    position: r.position,
    selected: opts.selected === r.key,
    data: { ref: r, taskName: opts.taskNames?.[r.taskId], status: opts.statuses?.[r.key], error: opts.errors?.[r.key] },
  }));
  const edges: FlowEdge[] = refs.flatMap((r) => r.dependsOn.map((d) => ({ id: edgeId(d, r.key), source: d, target: r.key })));
  return { nodes, edges };
}

/** Longest-path layering: a task sits one column right of its deepest dependency, so every edge
 *  points rightwards and independent branches stack vertically. Deterministic (input order breaks
 *  ties), and tolerant of a cyclic or dangling draft — it never throws, since it runs while the
 *  user is mid-edit. */
export function autoLayout(refs: TaskRef[]): TaskRef[] {
  const byKey = new Map(refs.map((r) => [r.key, r]));
  const depth = new Map<string, number>();
  const visiting = new Set<string>();
  const depthOf = (key: string): number => {
    const known = depth.get(key);
    if (known !== undefined) return known;
    if (visiting.has(key)) return 0; // a cycle: break it rather than recurse forever
    visiting.add(key);
    const r = byKey.get(key);
    const d = r ? Math.max(-1, ...r.dependsOn.filter((x) => byKey.has(x)).map(depthOf)) + 1 : 0;
    visiting.delete(key);
    depth.set(key, d);
    return d;
  };
  const rows = new Map<number, number>();
  return refs.map((r) => {
    const col = depthOf(r.key);
    const row = rows.get(col) ?? 0;
    rows.set(col, row + 1);
    return { ...r, position: { x: col * (NODE_WIDTH + COL_GAP), y: row * (NODE_HEIGHT + ROW_GAP) } };
  });
}

export function uniqueKey(prefix: string, existing: Iterable<string>): string {
  const taken = new Set(existing);
  for (let i = 1; ; i++) {
    const k = `${prefix}_${i}`;
    if (!taken.has(k)) return k;
  }
}

/** A new DAG node referencing `taskId` at its latest version (ADR 0071: a reference, never an
 *  embedded task definition — the task's own config lives and is edited separately). */
export function newRef(taskId: string, existing: TaskRef[], position?: { x: number; y: number }): TaskRef {
  return {
    key: uniqueKey("task", existing.map((r) => r.key)),
    taskId,
    taskVersion: "latest",
    dependsOn: [],
    position: position ?? nextFreePosition(existing),
  };
}

/** True when two or more references sit at the exact same position, so the canvas would draw them
 *  perfectly stacked — only the last one in the array is visible; the rest are in the DOM (not
 *  missing) but 100% hidden underneath it. Happens for a pipeline built directly against the API
 *  (e.g. a test fixture) whose references were never dragged in the builder: `position` defaults
 *  to `{x:0,y:0}` for every reference server-side (`model.py`), so they all land in the same spot. */
export function hasOverlappingPositions(refs: TaskRef[]): boolean {
  const seen = new Set<string>();
  for (const r of refs) {
    const key = `${r.position.x},${r.position.y}`;
    if (seen.has(key)) return true;
    seen.add(key);
  }
  return false;
}

/** Somewhere sensible for a new node: below whatever is already there. */
function nextFreePosition(existing: TaskRef[]): { x: number; y: number } {
  const maxY = existing.reduce((m, r) => Math.max(m, r.position.y), -(NODE_HEIGHT + ROW_GAP));
  return { x: 0, y: maxY + NODE_HEIGHT + ROW_GAP };
}

/** Whether the canvas should let a user draw an edge `from` -> `to` (from upstream to downstream).
 *  Mirrors the server's rules: no self-loops, no duplicates, no cycles. Returns the reason when
 *  refused, so the UI can say why. */
export function checkConnection(refs: TaskRef[], from: string, to: string): { ok: true } | { ok: false; reason: string } {
  const src = refs.find((r) => r.key === from);
  const dst = refs.find((r) => r.key === to);
  if (!src || !dst) return { ok: false, reason: "unknown task" };
  if (from === to) return { ok: false, reason: "a task cannot depend on itself" };
  if (dst.dependsOn.includes(from)) return { ok: false, reason: "already connected" };
  if (reaches(refs, to, from)) return { ok: false, reason: "that would create a cycle" };
  return { ok: true };
}

/** Is `target` downstream of (or equal to) `start`? */
function reaches(refs: TaskRef[], start: string, target: string): boolean {
  const down = new Map<string, string[]>();
  for (const r of refs) for (const d of r.dependsOn) down.set(d, [...(down.get(d) ?? []), r.key]);
  const seen = new Set<string>();
  const stack = [start];
  while (stack.length) {
    const k = stack.pop()!;
    if (k === target) return true;
    if (seen.has(k)) continue;
    seen.add(k);
    stack.push(...(down.get(k) ?? []));
  }
  return false;
}

export function connect(refs: TaskRef[], from: string, to: string): TaskRef[] {
  return refs.map((r) => (r.key === to ? { ...r, dependsOn: [...r.dependsOn, from] } : r));
}

export function disconnect(refs: TaskRef[], from: string, to: string): TaskRef[] {
  return refs.map((r) => (r.key === to ? { ...r, dependsOn: r.dependsOn.filter((d) => d !== from) } : r));
}

export function removeRef(refs: TaskRef[], key: string): TaskRef[] {
  return refs.filter((r) => r.key !== key).map((r) => ({ ...r, dependsOn: r.dependsOn.filter((d) => d !== key) }));
}

/** Rename a reference's key everywhere it is referenced. Keys are identifiers user code reads
 *  (ctx.inputs["<key>"]), so a rename is a real change to behaviour — the panel warns about it. */
export function renameKey(refs: TaskRef[], from: string, to: string): TaskRef[] {
  return refs.map((r) => {
    const dependsOn = r.dependsOn.map((d) => (d === from ? to : d));
    return r.key === from ? { ...r, key: to, dependsOn } : { ...r, dependsOn };
  });
}

/** A server error's `field` path, parsed relative to the task it names:
 *  "tasks[2].dependsOn" -> {index: 2, rest: "dependsOn"}
 *  "spec.tasks[0].taskId" -> {index: 0, rest: "taskId"}
 *  "tasks[1]" or "tasks" -> {index: 1, rest: null} / null (no field within the task, or no task at all)
 *
 *  `rest` keeps everything after "tasks[N]." verbatim, so a caller matches against the same
 *  literal strings the backend actually sends, not a guessed shorter form. */
export function parseTaskField(field: string | null | undefined): { index: number; rest: string | null } | null {
  const m = /(?:^|\.)tasks\[(\d+)\]\.?(.*)$/.exec(field ?? "");
  return m ? { index: Number(m[1]), rest: m[2] || null } : null;
}

export const KEY_RE = /^[a-z][a-z0-9_]{0,62}$/;
