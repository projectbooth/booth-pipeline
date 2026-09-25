import { useCallback, useMemo } from "react";
import {
  Background,
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
} from "@xyflow/react";
import { NODE_HEIGHT, NODE_WIDTH, checkConnection, connect, disconnect, removeRef, toFlow, type FlowNode } from "../graph";
import type { TaskRef, TaskStatus } from "../types";
import { StatusChip } from "./ui";

// The canvas is deliberately *controlled*: the `refs` array (a PipelineSpec's tasks — DAG-node
// references, ADR 0071) is the one source of truth, nodes and edges are derived from it on every
// render, and every gesture — drag, connect, delete — is translated back into a new `refs` array
// through the pure functions in graph.ts. There is no second copy of the graph inside React Flow
// to fall out of sync with what gets saved. Passing no `onChange` makes it read-only (run view,
// viewer role, an old version).

type TaskNodeData = FlowNode["data"] & Record<string, unknown>;

function TaskNode({ data, selected }: NodeProps<Node<TaskNodeData>>) {
  const { ref, taskName, status, error } = data;
  const versionLabel = ref.taskVersion === "latest" ? "latest" : `v${ref.taskVersion}`;
  return (
    <div
      data-testid={`task-node-${ref.key}`}
      aria-label={`Task ${taskName || ref.key}, ${versionLabel}${status ? `, ${status}` : ""}${error ? `, has an error: ${error}` : ""}`}
      className={`relative flex h-full w-full overflow-hidden rounded-md border bg-white text-left shadow-sm dark:bg-slate-900 ${
        error
          ? "border-red-500 ring-2 ring-red-400"
          : selected
            ? "border-indigo-500 ring-2 ring-indigo-400"
            : "border-slate-300 dark:border-slate-600"
      }`}
    >
      <div className="w-1.5 shrink-0 bg-indigo-500" aria-hidden="true" />
      <Handle type="target" position={Position.Left} className="!h-3 !w-3 !bg-slate-500" />
      <div className="flex min-w-0 flex-1 flex-col justify-center gap-0.5 px-2.5 py-1.5">
        <div className="flex items-center justify-between gap-2">
          <span className="truncate text-sm font-semibold text-slate-900 dark:text-slate-100">{taskName || "(no task picked)"}</span>
          {status && <StatusChip status={status} />}
        </div>
        <span className="truncate font-mono text-xs text-slate-500 dark:text-slate-400">{ref.key}</span>
        <span className="truncate text-xs text-slate-500 dark:text-slate-400">{versionLabel}</span>
      </div>
      <Handle type="source" position={Position.Right} className="!h-3 !w-3 !bg-slate-500" />
    </div>
  );
}

const nodeTypes: NodeTypes = { task: TaskNode };

export interface DagCanvasProps {
  refs: TaskRef[];
  selected: string | null;
  onSelect: (key: string | null) => void;
  /** Provide to make the canvas editable; omit for read-only. */
  onChange?: (refs: TaskRef[]) => void;
  taskNames?: Record<string, string>;
  statuses?: Record<string, TaskStatus>;
  errors?: Record<string, string>;
  theme?: "dark" | "light";
}

export function DagCanvas({ refs, selected, onSelect, onChange, taskNames, statuses, errors, theme = "light" }: DagCanvasProps) {
  const editable = onChange !== undefined;

  const { nodes, edges } = useMemo(() => {
    const flow = toFlow(refs, { taskNames, statuses, errors, selected });
    return {
      // A fixed node size means React Flow never has to measure a node, so nothing re-lays-out
      // (or flickers) when we regenerate these objects from `refs` on each render.
      nodes: flow.nodes.map((n) => ({ ...n, width: NODE_WIDTH, height: NODE_HEIGHT, data: n.data as TaskNodeData })),
      edges: flow.edges.map((e) => ({
        ...e,
        markerEnd: { type: MarkerType.ArrowClosed },
        animated: statuses?.[e.target] === "running",
      })),
    };
  }, [refs, taskNames, statuses, errors, selected]);

  const onNodesChange = useCallback(
    (changes: NodeChange[]) => {
      let next = refs;
      let changed = false;
      for (const c of changes) {
        if (c.type === "select" && c.selected) onSelect(c.id);
        else if (c.type === "position" && c.position && editable) {
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
    // A fixed height, not `h-full`: React Flow's own root (and this div) need a size that's
    // correct on the very first layout pass, not one that depends on resolving a percentage
    // against the parent's own flex-computed height — see agent-briefs/pipeline.md's live-test
    // discrepancy (worked in some sessions, reproducibly blank in another) for why this isn't
    // hypothetical. Matches the canvas wrapper's own `h-[32rem]` in PipelineEditor.tsx exactly.
    <div className="h-[32rem] w-full" data-testid="dag-canvas">
      <ReactFlow
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
        fitViewOptions={{ padding: 0.25, maxZoom: 1 }}
        minZoom={0.3}
      >
        <Background gap={20} />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}
