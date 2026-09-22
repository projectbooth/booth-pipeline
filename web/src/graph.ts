// Pure graph logic for the builder: converting between a PipelineSpec and what the canvas draws,
// what a new node looks like, and which connections the canvas should allow. Kept free of React
// and of @xyflow/react so it is trivially unit-testable — the canvas component is thin glue over
// this. The SERVER remains the authority (src/booth_pipeline/model.py validate_structure): these
// rules exist so the canvas never lets a user draw something that could not possibly be saved,
// not to replace the server's check.

import type { Task, TaskKind, TaskStatus } from "./types";

export const NODE_WIDTH = 220;
export const NODE_HEIGHT = 84;
const COL_GAP = 90;
const ROW_GAP = 40;

export interface FlowNode {
  id: string;
  type: "task";
  position: { x: number; y: number };
  data: { task: Task; status?: TaskStatus; error?: string };
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
  tasks: Task[],
  opts: { statuses?: Record<string, TaskStatus>; errors?: Record<string, string>; selected?: string | null } = {},
): { nodes: FlowNode[]; edges: FlowEdge[] } {
  const nodes: FlowNode[] = tasks.map((t) => ({
    id: t.key,
    type: "task",
    position: t.position,
    selected: opts.selected === t.key,
    data: { task: t, status: opts.statuses?.[t.key], error: opts.errors?.[t.key] },
  }));
  const edges: FlowEdge[] = tasks.flatMap((t) => t.dependsOn.map((d) => ({ id: edgeId(d, t.key), source: d, target: t.key })));
  return { nodes, edges };
}

/** Longest-path layering: a task sits one column right of its deepest dependency, so every edge
 *  points rightwards and independent branches stack vertically. Deterministic (input order breaks
 *  ties), and tolerant of a cyclic or dangling draft — it never throws, since it runs while the
 *  user is mid-edit. */
export function autoLayout(tasks: Task[]): Task[] {
  const byKey = new Map(tasks.map((t) => [t.key, t]));
  const depth = new Map<string, number>();
  const visiting = new Set<string>();
  const depthOf = (key: string): number => {
    const known = depth.get(key);
    if (known !== undefined) return known;
    if (visiting.has(key)) return 0; // a cycle: break it rather than recurse forever
    visiting.add(key);
    const t = byKey.get(key);
    const d = t ? Math.max(-1, ...t.dependsOn.filter((x) => byKey.has(x)).map(depthOf)) + 1 : 0;
    visiting.delete(key);
    depth.set(key, d);
    return d;
  };
  const rows = new Map<number, number>();
  return tasks.map((t) => {
    const col = depthOf(t.key);
    const row = rows.get(col) ?? 0;
    rows.set(col, row + 1);
    return { ...t, position: { x: col * (NODE_WIDTH + COL_GAP), y: row * (NODE_HEIGHT + ROW_GAP) } };
  });
}

// Starter templates. `kind` is purely decorative now (ADR 0062) — these are just friendlier
// example content for whichever label an author happens to pick; nothing about the shape of a
// task depends on them, and a task with no kind gets the generic one.
const GENERIC_TEMPLATE = `def run(ctx):
    # ctx.inputs maps each upstream task's key to what it returned; ctx.params are this task's
    # configured parameters. The return value (anything JSON-serialisable) becomes this task's
    # output, available to anything that depends on it.
    return None
`;
const KIND_TEMPLATES: Partial<Record<TaskKind, string>> = {
  source: `def run(ctx):
    print("reading source data")
    return [{"id": 1}, {"id": 2}, {"id": 3}]
`,
  transform: `def run(ctx):
    rows = [row for upstream in ctx.inputs.values() for row in upstream]
    print(f"transforming {len(rows)} rows")
    return rows
`,
  sink: `def run(ctx):
    rows = [row for upstream in ctx.inputs.values() for row in upstream]
    print(f"writing {len(rows)} rows")
`,
};

export function uniqueKey(prefix: string, existing: Iterable<string>): string {
  const taken = new Set(existing);
  for (let i = 1; ; i++) {
    const k = `${prefix}_${i}`;
    if (!taken.has(k)) return k;
  }
}

/** A new task. `kind` is an optional hint (ADR 0062: purely decorative, never required) used only
 *  to pick a nicer starter key/template — omit it for the generic "+ Add task" action. */
export function newTask(existing: Task[], kind: TaskKind | null = null, position?: { x: number; y: number }): Task {
  const key = uniqueKey(kind ?? "task", existing.map((t) => t.key));
  return {
    key,
    name: "",
    kind,
    code: { type: "inline", source: (kind && KIND_TEMPLATES[kind]) || GENERIC_TEMPLATE },
    runner: "base",
    retry: null,
    dependsOn: [],
    params: {},
    timeoutSeconds: 3600,
    platformAccess: false, // opt-in: a task gets a platform token only if it asks (least privilege)
    position: position ?? nextFreePosition(existing),
  };
}

/** Somewhere sensible for a new node: the column matching its likely role, below what is there. */
function nextFreePosition(existing: Task[]): { x: number; y: number } {
  const maxY = existing.reduce((m, t) => Math.max(m, t.position.y), -(NODE_HEIGHT + ROW_GAP));
  return { x: 0, y: maxY + NODE_HEIGHT + ROW_GAP };
}

/** Whether the canvas should let a user draw an edge `from` -> `to` (from upstream to downstream).
 *  Mirrors the server's rules (ADR 0062: `kind` plays no part in this — it's a label, not a
 *  topology constraint): no self-loops, no duplicates, no cycles. Returns the reason when refused,
 *  so the UI can say why. */
export function checkConnection(tasks: Task[], from: string, to: string): { ok: true } | { ok: false; reason: string } {
  const src = tasks.find((t) => t.key === from);
  const dst = tasks.find((t) => t.key === to);
  if (!src || !dst) return { ok: false, reason: "unknown task" };
  if (from === to) return { ok: false, reason: "a task cannot depend on itself" };
  if (dst.dependsOn.includes(from)) return { ok: false, reason: "already connected" };
  if (reaches(tasks, to, from)) return { ok: false, reason: "that would create a cycle" };
  return { ok: true };
}

/** Is `target` downstream of (or equal to) `start`? */
function reaches(tasks: Task[], start: string, target: string): boolean {
  const down = new Map<string, string[]>();
  for (const t of tasks) for (const d of t.dependsOn) down.set(d, [...(down.get(d) ?? []), t.key]);
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

export function connect(tasks: Task[], from: string, to: string): Task[] {
  return tasks.map((t) => (t.key === to ? { ...t, dependsOn: [...t.dependsOn, from] } : t));
}

export function disconnect(tasks: Task[], from: string, to: string): Task[] {
  return tasks.map((t) => (t.key === to ? { ...t, dependsOn: t.dependsOn.filter((d) => d !== from) } : t));
}

export function removeTask(tasks: Task[], key: string): Task[] {
  return tasks.filter((t) => t.key !== key).map((t) => ({ ...t, dependsOn: t.dependsOn.filter((d) => d !== key) }));
}

/** Rename a task's key everywhere it is referenced. Keys are identifiers user code reads
 *  (ctx.inputs["<key>"]), so a rename is a real change to behaviour — the panel warns about it. */
export function renameKey(tasks: Task[], from: string, to: string): Task[] {
  return tasks.map((t) => {
    const dependsOn = t.dependsOn.map((d) => (d === from ? to : d));
    return t.key === from ? { ...t, key: to, dependsOn } : { ...t, dependsOn };
  });
}

/** The task index a server error's `field` path refers to ("tasks[2].dependsOn" -> 2), or null. */
export function taskIndexOfField(field: string | null | undefined): number | null {
  const m = /(?:^|\.)tasks\[(\d+)\]/.exec(field ?? "");
  return m ? Number(m[1]) : null;
}

export const KEY_RE = /^[a-z][a-z0-9_]{0,62}$/;
