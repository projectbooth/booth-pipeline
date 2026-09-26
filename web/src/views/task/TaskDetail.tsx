import type { ViewCtx } from "../../context";
import type { TaskTab } from "../../navigation";
import { Banner } from "../../ui/primitives";

// Placeholder until ADR 0074 phase 4 (docs/decisions/0016 §8) replaces it with S5.
export function TaskDetail({ taskId, tab }: { v: ViewCtx; taskId: string; tab: TaskTab; version?: number }) {
  return <Banner tone="info">Task {taskId} ({tab}) — being rebuilt, arrives in phase 4.</Banner>;
}
