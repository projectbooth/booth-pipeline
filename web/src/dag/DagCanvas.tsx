import { useCallback, useEffect, useMemo, useRef, type MutableRefObject, type ReactNode } from "react";
import {
  Background,
  BackgroundVariant,
  Controls,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  type Connection,
  type EdgeChange,
  type Node,
  type NodeChange,
  type NodeProps,
  type NodeTypes,
  useReactFlow,
  useStore,
} from "@xyflow/react";
import { NODE_HEIGHT, NODE_WIDTH, checkConnection, connect, disconnect, removeRef } from "../graph";
import type { TaskRef, TaskStatus } from "../types";
import { STATUS_STYLE } from "../ui/primitives";
import { languageOf, versionRefLabel, type Resolved } from "./resolve";

// The DAG canvas (docs/decisions/0016 §4 S2a/S3, §5, §6).
//
// LAYOUT — the reason this file exists in this shape. Every earlier canvas bug (blank below `lg`,
// blank at ≥1024px, collapsing on node selection) came from the canvas's height being *derived*:
// a flex item in a row/column-switching container, sharing an axis with a side panel, with an
// `h-full` percentage chain down into React Flow. Here the height is never derived:
//   1. The wrapper is a plain block element with an explicit pixel height (the `height` prop).
//      It must never be made a flex/grid item whose size depends on siblings.
//   2. React Flow fills it at 100% × 100% — one resolution step against a definite number.
//   3. Anything that "opens" for a node (panels, pickers) is rendered by the caller AFTER this
//      element in normal flow, never beside or over it.
//   4. The view is fitted to the nodes when React Flow initialises, and again whenever React
//      Flow's own measured size changes (FitOnResize) — until the user pans or zooms, after which
//      their view is left alone. Fitting only at init was not enough: live-tested, React Flow can
//      initialise before the surrounding layout settles and fit to a box ~40% too narrow.
//
// DATA — the canvas is controlled: `refs` is the single source of truth, nodes/edges are derived
// from it every render, and every gesture is translated back into a new `refs` array via the pure
// functions in graph.ts. Passing no `onChange` makes it read-only.

export const CANVAS_HEIGHT = 560;

const FIT = { padding: 0.2, maxZoom: 1 };

interface TaskNodeData extends Record<string, unknown> {
  ref: TaskRef;
  name: string | null;
  lang: string | null;
  status?: TaskStatus;
  error?: string;
  connectable: boolean;
}

function TaskNode({ data, selected }: NodeProps<Node<TaskNodeData>>) {
  const { ref, name, lang, status, error, connectable } = data;
  const style = status ? STATUS_STYLE[status] : null;
  const border = error ? "border-red-500" : style ? style.border : "border-slate-300 dark:border-slate-600";
  const ring = error ? "ring-2 ring-red-400/70" : selected ? "ring-2 ring-indigo-500" : "";
  return (
    <div
      data-testid={`task-node-${ref.key}`}
      data-status={status ?? "none"}
      aria-label={`Task ${name ?? ref.key}, ${versionRefLabel(ref)}${status ? `, ${STATUS_STYLE[status].label.toLowerCase()}` : ""}${error ? `, has an error: ${error}` : ""}`}
      className={`flex h-full w-full flex-col justify-center gap-1 rounded-lg border-2 bg-white px-3 py-2 text-left shadow-sm dark:bg-slate-900 ${border} ${ring}`}
    >
      <Handle type="target" position={Position.Left} isConnectable={connectable} className="!h-2.5 !w-2.5 !border-slate-400 !bg-white dark:!bg-slate-800" />
      <div className="flex items-center justify-between gap-2">
        <span className="rounded bg-slate-100 px-1.5 py-px font-mono text-[10px] font-semibold tracking-wide text-slate-600 dark:bg-slate-800 dark:text-slate-300">
          {lang ?? "TASK"}
        </span>
        {style && <span title={style.label} className={`h-2.5 w-2.5 rounded-full ${style.dot}`} aria-hidden="true" />}
      </div>
      <span className="truncate text-sm font-semibold text-slate-900 dark:text-slate-50">{name ?? <span className="italic text-slate-400">loading…</span>}</span>
      <span className="truncate font-mono text-[11px] text-slate-500 dark:text-slate-400">
        {ref.key} · {versionRefLabel(ref)}
      </span>
      <Handle type="source" position={Position.Right} isConnectable={connectable} className="!h-2.5 !w-2.5 !border-slate-400 !bg-white dark:!bg-slate-800" />
    </div>
  );
}

const nodeTypes: NodeTypes = { task: TaskNode };

/** Re-fits whenever React Flow's OWN measured size changes, until the user moves the view.
 *  Keyed on the store's width/height rather than an outside ResizeObserver on purpose: fitView
 *  computes against those stored dimensions, so fitting from anywhere else can race React Flow's
 *  own measurement and fit to a stale size (seen live: 0.46× instead of ~0.78×). */
function FitOnResize({ userMoved }: { userMoved: MutableRefObject<boolean> }) {
  const width = useStore((st) => st.width);
  const height = useStore((st) => st.height);
  const { fitView } = useReactFlow();
  useEffect(() => {
    if (width > 0 && height > 0 && !userMoved.current) void fitView(FIT);
  }, [width, height, fitView, userMoved]);
  return null;
}

export interface DagCanvasProps {
  refs: TaskRef[];
  resolved: Resolved;
  selected: string | null;
  onSelect: (key: string | null) => void;
  /** Provide to make the canvas editable; omit for read-only. */
  onChange?: (refs: TaskRef[]) => void;
  statuses?: Record<string, TaskStatus>;
  errors?: Record<string, string>;
  theme: "dark" | "light";
  height?: number;
  label: string;
  /** Drawn centred over an empty canvas (inside the fixed-height box, so it can't change its size). */
  empty?: ReactNode;
}

export function DagCanvas({ refs, resolved, selected, onSelect, onChange, statuses, errors, theme, height = CANVAS_HEIGHT, label, empty }: DagCanvasProps) {
  const editable = onChange !== undefined;

  // Once the user pans or zooms, stop re-fitting their view (see LAYOUT 4 above).
  const userMoved = useRef(false);

  const nodes = useMemo(
    () =>
      refs.map((r) => ({
        id: r.key,
        type: "task",
        position: r.position,
        selected: selected === r.key,
        // A fixed node size means React Flow never measures a node, so nothing re-lays-out (or
        // flickers) when these objects are regenerated from `refs` on each render.
        width: NODE_WIDTH,
        height: NODE_HEIGHT,
        data: {
          ref: r,
          name: resolved[r.key]?.task?.name ?? (resolved[r.key]?.error ? "(task missing)" : null),
          lang: languageOf(resolved[r.key]?.config ?? null),
          status: statuses?.[r.key],
          error: errors?.[r.key],
          connectable: editable,
        } satisfies TaskNodeData,
      })),
    [refs, resolved, statuses, errors, selected, editable],
  );
  const edges = useMemo(
    () =>
      refs.flatMap((r) =>
        r.dependsOn.map((d) => ({
          id: `${d}->${r.key}`,
          source: d,
          target: r.key,
          markerEnd: { type: MarkerType.ArrowClosed },
          animated: statuses?.[r.key] === "running",
        })),
      ),
    [refs, statuses],
  );

  const onNodesChange = useCallback(
    (changes: NodeChange[]) => {
      let next = refs;
      let changed = false;
      for (const c of changes) {
        if (c.type === "position" && c.position && editable) {
          const pos = c.position;
          next = next.map((r) => (r.key === c.id ? { ...r, position: { x: Math.round(pos.x), y: Math.round(pos.y) } } : r));
          changed = true;
        } else if (c.type === "remove" && editable) {
          next = removeRef(next, c.id);
          changed = true;
          onSelect(null);
        }
      }
      if (changed) onChange?.(next);
    },
    [refs, editable, onChange, onSelect],
  );

  const onEdgesChange = useCallback(
    (changes: EdgeChange[]) => {
      if (!editable) return;
      let next = refs;
      let changed = false;
      for (const c of changes) {
        if (c.type !== "remove") continue;
        const [from, to] = c.id.split("->");
        next = disconnect(next, from, to);
        changed = true;
      }
      if (changed) onChange?.(next);
    },
    [refs, editable, onChange],
  );

  const onConnect = useCallback(
    (c: Connection) => {
      if (!editable || !c.source || !c.target) return;
      if (checkConnection(refs, c.source, c.target).ok) onChange?.(connect(refs, c.source, c.target));
    },
    [refs, editable, onChange],
  );

  return (
    <div
      role="region"
      aria-label={label}
      data-testid="dag-canvas"
      // Explicit pixel height, block layout — see the LAYOUT note at the top of this file.
      style={{ height, position: "relative" }}
      className="w-full overflow-hidden rounded-lg border border-slate-200 bg-slate-50 dark:border-slate-700 dark:bg-slate-950"
    >
      <ReactFlow
        style={{ width: "100%", height: "100%" }}
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onConnect={onConnect}
        isValidConnection={(c) => !!c.source && !!c.target && checkConnection(refs, c.source, c.target).ok}
        onNodeClick={(_, n) => onSelect(n.id)}
        onPaneClick={() => onSelect(null)}
        nodesDraggable={editable}
        nodesConnectable={editable}
        elementsSelectable
        deleteKeyCode={editable ? ["Backspace", "Delete"] : null}
        colorMode={theme}
        fitView
        fitViewOptions={FIT}
        // Only a real user gesture carries an event; programmatic fits don't.
        onMoveStart={(event) => {
          if (event) userMoved.current = true;
        }}
        minZoom={0.3}
      >
        <Background variant={BackgroundVariant.Dots} gap={20} size={1.2} />
        <Controls showInteractive={false} />
        <FitOnResize userMoved={userMoved} />
      </ReactFlow>
      {refs.length === 0 && empty && <div className="pointer-events-none absolute inset-0 flex items-center justify-center p-6">{empty}</div>}
    </div>
  );
}
