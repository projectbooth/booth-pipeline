import { api, type ApiContext } from "./api/client";
import type { Run, RunStatus } from "./types";

// Run-derived facts the screens show about a pipeline (docs/decisions/0016 §3): its status badge,
// last run and duration all come from its most recent run. The backend has no "latest run per
// pipeline" query, so this assembles one from the runs listing — newest first in both stores
// (`ORDER BY created_at DESC, id DESC`).

export const ACTIVE_RUN: RunStatus[] = ["queued", "running"];

export const isActive = (r: Pick<Run, "status"> | null | undefined): boolean => !!r && ACTIVE_RUN.includes(r.status);

const BULK_LIMIT = 200; // the API's MAX_PAGE
const FALLBACK_CONCURRENCY = 6;

/** The most recent run of each pipeline in `pipelineIds` (null: it has never run).
 *
 *  One bulk request covers every pipeline that ran recently. Any pipeline absent from it gets its
 *  own `limit=1` query rather than being reported "never run" just because its last run is older
 *  than the newest 200 in the workspace — the status column must not be wrong for a quiet pipeline. */
export async function latestRunsFor(ctx: ApiContext, pipelineIds: string[]): Promise<Record<string, Run | null>> {
  const out: Record<string, Run | null> = {};
  if (pipelineIds.length === 0) return out;
  const wanted = new Set(pipelineIds);
  const bulk = await api.listRuns(ctx, { limit: BULK_LIMIT });
  for (const r of bulk.items) if (wanted.has(r.pipelineId) && !(r.pipelineId in out)) out[r.pipelineId] = r;

  // If the bulk page already holds every run in the workspace, absence really does mean "never run".
  if (bulk.total <= bulk.items.length) {
    for (const id of pipelineIds) if (!(id in out)) out[id] = null;
    return out;
  }
  const missing = pipelineIds.filter((id) => !(id in out));
  await mapLimit(missing, FALLBACK_CONCURRENCY, async (id) => {
    const page = await api.listRuns(ctx, { pipelineId: id, limit: 1 });
    out[id] = page.items[0] ?? null;
  });
  return out;
}

async function mapLimit<T>(items: T[], limit: number, fn: (item: T) => Promise<void>): Promise<void> {
  let next = 0;
  const workers = Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (next < items.length) await fn(items[next++]);
  });
  await Promise.all(workers);
}

/** When a run actually started (falls back to when it was queued). */
export const runStart = (r: Run): string => r.startedAt ?? r.createdAt;
