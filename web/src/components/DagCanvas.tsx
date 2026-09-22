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
import { NODE_HEIGHT, NODE_WIDTH, checkConnection, connect, disconnect, removeTask, toFlow, type FlowNode } from "../graph";
import type { Task, TaskKind, TaskStatus } from "../types";
import { StatusChip } from "./ui";

// The canvas is deliberately *controlled*: the `tasks` array (a PipelineSpec's tasks) is the one
// source of truth, nodes and edges are derived from it on every render, and every gesture — drag,
// connect, delete — is translated back into a new `tasks` array through the pure functions in
// graph.ts. There is no second copy of the graph inside React Flow to fall out of sync with what
// gets saved. Passing no `onChange` makes it read-only (run view, viewer role, an old version).

// Purely decorative (ADR 0062): `kind` is a label, never a constraint on which handles a task
// gets — every task has both a target and a source handle regardless of kind (or no kind at all),
// since dependency wiring no longer has anything to do with it.
const KIND_STYLE: Record<TaskKind, { bar: string; label: string }> = {
  source: { bar: "bg-sky-500", label: "Source" },
  transform: { bar: "bg-indigo-500", label: "Transform" },
  sink: { bar: "bg-emerald-500", label: "Sink" },
};
const UNTAGGED_STYLE = { bar: "bg-slate-400 dark:bg-slate-500", label: "Task" };

type TaskNodeData = FlowNode["data"] & Record<string, unknown>;

function codeLabel(task: Task): string {
  if (task.code.type === "inline") return "inline code";
  if (task.code.type === "catalog") {
    if (!task.code.entryId) return "no code picked";
    return `${task.code.name ?? task.code.entryId} @ ${task.code.version}`;
  }
  return task.code.path ? (task.code.name ?? task.code.path) : "no code picked";
}

function TaskNode({ data, selected }: NodeProps<Node<TaskNodeData>>) {
  const { task, status, error } = data;
  const style = task.kind ? KIND_STYLE[task.kind] : UNTAGGED_STYLE;
  return (
    <div
      data-testid={`task-node-${task.key}`}
      aria-label={`${style.label} task ${task.name || task.key}${status ? `, ${status}` : ""}${error ? `, has an error: ${error}` : ""}`}
      className={`relative flex h-full w-full overflow-hidden rounded-md border bg-white text-left shadow-sm dark:bg-slate-900 ${
        error
          ? "border-red-500 ring-2 ring-red-400"
          : selected
            ? "border-indigo-500 ring-2 ring-indigo-400"
            : "border-slate-300 dark:border-slate-600"
      }`}
    >
      <div className={`w-1.5 shrink-0 ${style.bar}`} aria-hidden="true" />
      {/* Every task gets both handles: kind no longer says anything about whether connections are legal (ADR 0062). */}
      <Handle type="target" position={Position.Left} className="!h-3 !w-3 !bg-slate-500" />
      <div className="flex min-w-0 flex-1 flex-col justify-center gap-0.5 px-2.5 py-1.5">
        <div className="flex items-center justify-between gap-2">
          <span className="truncate text-sm font-semibold text-slate-900 dark:text-slate-100">{task.name || task.key}</span>
          {status && <StatusChip status={status} />}
        </div>
        <span className="truncate font-mono text-xs text-slate-500 dark:text-slate-400">{task.key}</span>
        <span className="truncate text-xs text-slate-500 dark:text-slate-400">
          {style.label} · {codeLabel(task)}
          {task.runner !== "base" ? ` · ${task.runner}` : ""}
          {task.retry && task.retry.maxRetries > 0 ? ` · ↻${task.retry.maxRetries}` : ""}
        </span>
      </div>
      <Handle type="source" position={Position.Right} className="!h-3 !w-3 !bg-slate-500" />
    </div>
  );
}

const nodeTypes: NodeTypes = { task: TaskNode };

export interface DagCanvasProps {
  tasks: Task[];
  selected: string | null;
  onSelect: (key: string | null) => void;
  /** Provide to make the canvas editable; omit for read-only. */
  onChange?: (tasks: Task[]) => void;
  statuses?: Record<string, TaskStatus>;
  errors?: Record<string, string>;
  theme?: "dark" | "light";
}

export function DagCanvas({ tasks, selected, onSelect, onChange, statuses, errors, theme = "light" }: DagCanvasProps) {
  const editable = onChange !== undefined;

  const { nodes, edges } = useMemo(() => {
    const flow = toFlow(tasks, { statuses, errors, selected });
    return {
      // A fixed node size means React Flow never has to measure a node, so nothing re-lays-out
      // (or flickers) when we regenerate these objects from `tasks` on each render.
      nodes: flow.nodes.map((n) => ({ ...n, width: NODE_WIDTH, height: NODE_HEIGHT, data: n.data as TaskNodeData })),
      edges: flow.edges.map((e) => ({
        ...e,
        markerEnd: { type: MarkerType.ArrowClosed },
        animated: statuses?.[e.target] === "running",
      })),
    };
  }, [tasks, statuses, errors, selected]);

  const onNodesChange = useCallback(
    (changes: NodeChange[]) => {
      let next = tasks;
      let changed = false;
      for (const c of changes) {
        if (c.type === "select" && c.selected) onSelect(c.id);
        else if (c.type === "position" && c.position && editable) {
          const pos = c.position;
          next = next.map((t) => (t.key === c.id ? { ...t, position: { x: Math.round(pos.x), y: Math.round(pos.y) } } : t));
          changed = true;
        } else if (c.type === "remove" && editable) {
          next = removeTask(next, c.id);
          changed = true;
          onSelect(null);
        }
      }
      if (changed) onChange?.(next);
    },
    [tasks, editable, onChange, onSelect],
  );

  const onEdgesChange = useCallback(
    (changes: EdgeChange[]) => {
      if (!editable) return;
      let next = tasks;
      let changed = false;
      for (const c of changes) {
        if (c.type !== "remove") continue;
        const [from, to] = c.id.split("->");
        next = disconnect(next, from, to);
        changed = true;
      }
      if (changed) onChange?.(next);
    },
    [tasks, editable, onChange],
  );

  const onConnect = useCallback(
    (c: Connection) => {
      if (!editable || !c.source || !c.target) return;
      if (checkConnection(tasks, c.source, c.target).ok) onChange?.(connect(tasks, c.source, c.target));
    },
    [tasks, editable, onChange],
  );

  return (
    <div className="h-full w-full" data-testid="dag-canvas">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onConnect={onConnect}
        isValidConnection={(c) => !!c.source && !!c.target && checkConnection(tasks, c.source, c.target).ok}
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
