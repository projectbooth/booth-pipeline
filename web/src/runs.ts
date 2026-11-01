import { api, type ApiContext } from "./api/client";
import type { Run, RunStatus } from "./types";

// Run-derived facts the screens show about a pipeline (docs/decisions/0016 §3): its status badge,
// last run and duration all come from its most recent run. The backend has no "latest run per
// pipeline" query, so this assembles one from the runs listing — newest first in both stores
// (`ORDER BY created_at DESC, id DESC`). One exception: a run still going beats a newer skipped row. A
// scheduled fire that finds the previous run active is recorded as a skipped run, so the newest row of a busy
// pipeline can be a drop, and showing it would hide the run that is actually executing.

export const ACTIVE_RUN: RunStatus[] = ["queued", "running"];

export const isActive = (r: Pick<Run, "status"> | null | undefined): boolean => !!r && ACTIVE_RUN.includes(r.status);

const BULK_LIMIT = 200; // the API's MAX_PAGE
const FALLBACK_CONCURRENCY = 6;

/** The queued and running runs, of one pipeline or of the workspace. */
async function activeRuns(ctx: ApiContext, pipelineId?: string): Promise<Run[]> {
  const pages = await Promise.all(ACTIVE_RUN.map((status) => api.listRuns(ctx, { pipelineId, status, limit: BULK_LIMIT })));
  return pages.flatMap((p) => p.items);
}

function newest(runs: Run[]): Run | null {
  return runs.reduce<Run | null>((best, r) => (!best || r.createdAt > best.createdAt ? r : best), null);
}

/** The run to show for one pipeline: its most recent, unless that is a skipped row and a run is still going. */
export async function latestRunFor(ctx: ApiContext, pipelineId: string): Promise<Run | null> {
  const page = await api.listRuns(ctx, { pipelineId, limit: 1 });
  const run = page.items[0] ?? null;
  return run?.status === "skipped" ? (newest(await activeRuns(ctx, pipelineId)) ?? run) : run;
}

export interface LatestRuns {
  latest: Record<string, Run | null>;
  /** Whether any of these pipelines has a run going, shown or not (concurrent runs can hide one behind a
   *  newer finished run). What keeps the list polling. */
  anyActive: boolean;
}

/** The run to show for each pipeline in `pipelineIds`: its most recent (null: it has never run), unless that
 *  is a skipped row and a run is still going.
 *
 *  One bulk request covers every pipeline that ran recently. Any pipeline absent from it gets its
 *  own `limit=1` query rather than being reported "never run" just because its last run is older
 *  than the newest 200 in the workspace — the status column must not be wrong for a quiet pipeline. */
export async function latestRunsFor(ctx: ApiContext, pipelineIds: string[]): Promise<LatestRuns> {
  const out: Record<string, Run | null> = {};
  if (pipelineIds.length === 0) return { latest: out, anyActive: false };
  const wanted = new Set(pipelineIds);
  // The active runs are asked for separately: a long run can sit behind more than a page of skipped rows.
  const [bulk, active] = await Promise.all([api.listRuns(ctx, { limit: BULK_LIMIT }), activeRuns(ctx)]);
  for (const r of bulk.items) if (wanted.has(r.pipelineId) && !(r.pipelineId in out)) out[r.pipelineId] = r;

  // If the bulk page already holds every run in the workspace, absence really does mean "never run".
  if (bulk.total <= bulk.items.length) {
    for (const id of pipelineIds) if (!(id in out)) out[id] = null;
  } else {
    const missing = pipelineIds.filter((id) => !(id in out));
    await mapLimit(missing, FALLBACK_CONCURRENCY, async (id) => {
      const page = await api.listRuns(ctx, { pipelineId: id, limit: 1 });
      out[id] = page.items[0] ?? null;
    });
  }
  const going = active.filter((r) => wanted.has(r.pipelineId));
  for (const id of pipelineIds) {
    if (out[id]?.status === "skipped") out[id] = newest(going.filter((r) => r.pipelineId === id)) ?? out[id];
  }
  return { latest: out, anyActive: going.length > 0 || Object.values(out).some(isActive) };
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
