import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../../api/client";
import type { ViewCtx } from "../../context";
import { formatDuration, formatRelative, formatStamp, formatTime } from "../../format";
import { errorMessage, useInterval } from "../../hooks";
import { isActive, runStart } from "../../runs";
import type { Pipeline, Run } from "../../types";
import {
  Banner,
  Button,
  EmptyState,
  Link,
  STATUS_STYLE,
  Skeleton,
  StatCard,
  StatusBadge,
  linkClass,
  rowHoverClass,
  tableClass,
  tbodyClass,
  tdClass,
  thClass,
  theadClass,
} from "../../ui/primitives";

// S2c — the Runs tab (docs/decisions/0016 §4, Figma screen 4). The stat cards are exact all-time
// counts straight from the server (`status=` filter + `.total`), not a sample of the loaded page.

const PAGE = 50;
const STRIP = 30;

interface Counts {
  total: number;
  succeeded: number;
  failed: number;
}

export function RunsTab({ v, pipeline }: { v: ViewCtx; pipeline: Pipeline }) {
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [total, setTotal] = useState(0);
  const [counts, setCounts] = useState<Counts | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const shown = useRef(PAGE);

  const refresh = useCallback(async () => {
    try {
      const [page, ok, failed] = await Promise.all([
        api.listRuns(v.api, { pipelineId: pipeline.id, limit: shown.current }),
        api.listRuns(v.api, { pipelineId: pipeline.id, status: "succeeded", limit: 1 }),
        api.listRuns(v.api, { pipelineId: pipeline.id, status: "failed", limit: 1 }),
      ]);
      setRuns(page.items);
      setTotal(page.total);
      setCounts({ total: page.total, succeeded: ok.total, failed: failed.total });
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    }
  }, [v.api, pipeline.id]);

  useEffect(() => {
    void refresh();
  }, [refresh]);
  useInterval(() => void refresh(), 3000, !!runs?.some(isActive));

  async function loadMore() {
    if (!runs) return;
    setLoadingMore(true);
    try {
      const page = await api.listRuns(v.api, { pipelineId: pipeline.id, limit: PAGE, offset: runs.length });
      setRuns([...runs, ...page.items]);
      shown.current = runs.length + page.items.length;
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoadingMore(false);
    }
  }

  if (error && !runs) return <Banner tone="error" action={<Button size="sm" onClick={() => void refresh()}>Retry</Button>}>{error}</Banner>;
  if (!runs || !counts) {
    return (
      <div role="status" aria-label="Loading" className="flex flex-col gap-3">
        <Skeleton className="h-20 w-full" />
        <Skeleton className="h-40 w-full" />
      </div>
    );
  }
  if (runs.length === 0) return <EmptyState title="No runs yet">Use Run now above, or give it a schedule.</EmptyState>;

  const finished = counts.succeeded + counts.failed;
  const rate = finished === 0 ? null : Math.round((counts.succeeded / finished) * 100);
  const strip = runs.slice(0, STRIP).reverse(); // oldest → newest, left to right

  return (
    <div className="flex flex-col gap-4">
      {error && <Banner tone="error">{error}</Banner>}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3 lg:grid-cols-[repeat(3,minmax(9rem,11rem))_1fr]">
        <StatCard label="Success rate" value={rate === null ? "—" : `${rate}%`} tone={rate === null ? "slate" : rate >= 90 ? "emerald" : rate >= 50 ? "amber" : "red"}>
          <p className="mt-0.5 text-xs text-slate-500 dark:text-slate-400" title="Succeeded ÷ (succeeded + failed). Canceled and in-flight runs are left out.">
            of {finished} finished
          </p>
        </StatCard>
        <StatCard label="Total runs" value={counts.total} tone="indigo" />
        <StatCard label="Failed runs" value={counts.failed} tone={counts.failed > 0 ? "red" : "slate"} />
        <div className="min-w-0 rounded-lg border border-slate-200 bg-white px-4 py-3 sm:col-span-3 lg:col-span-1 dark:border-slate-700 dark:bg-slate-900">
          <p className="text-[11px] font-semibold uppercase tracking-wider text-slate-500 dark:text-slate-400">History · last {strip.length}</p>
          <ol aria-label="Run history, oldest to newest" className="mt-2 flex h-8 items-stretch gap-1">
            {strip.map((r) => (
              <li key={r.id} className="min-w-[6px] flex-1">
                <Link
                  href={v.href({ name: "run", id: r.id })}
                  onNavigate={v.goPath}
                  title={`${STATUS_STYLE[r.status].label} · ${formatTime(runStart(r))} · ${r.finishedAt ? formatDuration(r.startedAt, r.finishedAt) : "in progress"}`}
                  aria-label={`Run ${r.id.slice(0, 8)}, ${STATUS_STYLE[r.status].label.toLowerCase()}, ${formatStamp(runStart(r))}`}
                  className={`block h-full rounded-sm ${STATUS_STYLE[r.status].bar} hover:opacity-80`}
                />
              </li>
            ))}
          </ol>
        </div>
      </div>

      <div className="relative overflow-x-auto">
        <table className={tableClass}>
          <thead className={theadClass}>
            <tr>
              <th className={thClass}>Run</th>
              <th className={thClass}>Status</th>
              <th className={thClass}>Version</th>
              <th className={thClass}>Started</th>
              <th className={thClass}>Duration</th>
              <th className={thClass}>Triggered by</th>
            </tr>
          </thead>
          <tbody className={tbodyClass}>
            {runs.map((r) => (
              <tr key={r.id} className={rowHoverClass}>
                <td className={tdClass}>
                  <Link href={v.href({ name: "run", id: r.id })} onNavigate={v.goPath} className={`${linkClass} font-mono text-xs`}>
                    {r.id.slice(0, 8)}
                  </Link>
                </td>
                <td className={tdClass}>
                  <StatusBadge status={r.status} />
                </td>
                <td className={`${tdClass} font-mono text-xs`}>v{r.pipelineVersion}</td>
                <td className={`${tdClass} whitespace-nowrap font-mono text-xs`} title={formatRelative(runStart(r))}>
                  {formatStamp(runStart(r))}
                </td>
                <td className={`${tdClass} whitespace-nowrap font-mono text-xs`}>{r.finishedAt ? formatDuration(r.startedAt, r.finishedAt) : isActive(r) ? "running…" : "—"}</td>
                <td className={`${tdClass} text-slate-600 dark:text-slate-300`}>{r.trigger === "schedule" ? "Scheduled" : `${r.triggeredBy} (manual)`}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {runs.length < total && (
        <div>
          <Button onClick={loadMore} disabled={loadingMore}>
            {loadingMore ? "Loading…" : `Load more (${total - runs.length} older)`}
          </Button>
        </div>
      )}
    </div>
  );
}
