import type { ViewCtx } from "../../context";
import type { PipelineTab } from "../../navigation";
import { Banner } from "../../ui/primitives";

// Placeholder until ADR 0074 phase 2 (docs/decisions/0016 §8) replaces it with S2.
export function PipelineDetail({ pipelineId, tab }: { v: ViewCtx; pipelineId: string; tab: PipelineTab; version?: number }) {
  return <Banner tone="info">Pipeline {pipelineId} ({tab}) — being rebuilt, arrives in phase 2.</Banner>;
}
