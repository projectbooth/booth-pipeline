import { useState } from "react";
import type { ViewCtx } from "../../context";
import { formatRelative, formatStamp, formatTime } from "../../format";
import { errorMessage } from "../../hooks";
import { Badge, Banner, Button, EmptyState, Link, linkClass, rowHoverClass, tableClass, tbodyClass, tdClass, thClass, theadClass } from "../../ui/primitives";
import { downloadYaml, type PipelineData } from "./data";

// S2f — the Versions tab (docs/decisions/0016 §4): the pipeline's own version history, with
// creation times, reached from the pipeline rather than cluttering the list (ADR 0074 parity).

export function VersionsTab({ v, d }: { v: ViewCtx; d: PipelineData }) {
  const p = d.pipeline;
  const [error, setError] = useState<string | null>(null);
  const [exporting, setExporting] = useState<number | null>(null);

  async function exportYaml(version: number) {
    setExporting(version);
    setError(null);
    try {
      await downloadYaml(v.api, p, version);
    } catch (err) {
      setError(`Export failed: ${errorMessage(err)}`);
    } finally {
      setExporting(null);
    }
  }

  if (d.versions.length === 0 && !d.draft) {
    return <EmptyState title="No versions yet">Save the DAG as a version to make it runnable and pinnable.</EmptyState>;
  }

  return (
    <div className="flex flex-col gap-3">
      {error && <Banner tone="error">{error}</Banner>}
      <div className="relative overflow-x-auto">
        <table className={tableClass}>
          <thead className={theadClass}>
            <tr>
              <th className={thClass}>Version</th>
              <th className={thClass}>Created</th>
              <th className={thClass}>By</th>
              <th className={thClass}>Tasks</th>
              <th className={thClass}>Notes</th>
              <th className={thClass}>
                <span className="sr-only">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody className={tbodyClass}>
            {d.draft && d.draftDiffers && (
              <tr className={rowHoverClass}>
                <td className={tdClass}>
                  <Badge tone="sky">Draft</Badge>
                </td>
                <td className={`${tdClass} font-mono text-xs`} title={formatTime(d.draft.updatedAt)}>
                  saved {formatRelative(d.draft.updatedAt)}
                </td>
                <td className={tdClass}>{d.draft.updatedBy}</td>
                <td className={`${tdClass} font-mono text-xs`}>{d.draft.spec.tasks.length}</td>
                <td className={`${tdClass} text-slate-500 dark:text-slate-400`}>{d.latest ? `differs from v${d.latest.version}` : "never saved as a version"}</td>
                <td className={`${tdClass} text-right`}>
                  <Link href={v.href({ name: "pipeline", id: p.id })} onNavigate={v.goPath} className={linkClass}>
                    Open
                  </Link>
                </td>
              </tr>
            )}
            {d.versions.map((ver) => (
              <tr key={ver.version} className={rowHoverClass}>
                <td className={`${tdClass} whitespace-nowrap`}>
                  <span className="font-mono text-sm font-medium">v{ver.version}</span>{" "}
                  {ver.version === p.latestVersion && <Badge tone="indigo">latest</Badge>} {ver.version === p.pinnedVersion && <Badge tone="amber">pinned</Badge>}
                </td>
                <td className={`${tdClass} whitespace-nowrap font-mono text-xs`} title={`${formatTime(ver.createdAt)} (${formatRelative(ver.createdAt)})`}>
                  {formatStamp(ver.createdAt)}
                </td>
                <td className={tdClass}>{ver.createdBy}</td>
                <td className={`${tdClass} font-mono text-xs`}>{ver.taskCount}</td>
                <td className={`${tdClass} max-w-sm truncate text-slate-600 dark:text-slate-300`}>{ver.notes || "—"}</td>
                <td className={`${tdClass} whitespace-nowrap text-right`}>
                  <span className="inline-flex items-center gap-3">
                    <Link href={v.href({ name: "pipeline", id: p.id, version: ver.version })} onNavigate={v.goPath} className={linkClass}>
                      Open
                    </Link>
                    <Button size="sm" onClick={() => exportYaml(ver.version)} disabled={exporting !== null} aria-label={`Export v${ver.version} as YAML`}>
                      {exporting === ver.version ? "Exporting…" : "Export YAML"}
                    </Button>
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
