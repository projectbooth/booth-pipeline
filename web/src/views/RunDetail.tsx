import type { ViewCtx } from "../context";
import { Banner } from "../ui/primitives";

// Placeholder until ADR 0074 phase 2 (docs/decisions/0016 §8) replaces it with S3.
export function RunDetail({ runId }: { v: ViewCtx; runId: string; initialTask?: string }) {
  return <Banner tone="info">Run {runId} — being rebuilt, arrives in phase 2.</Banner>;
}
