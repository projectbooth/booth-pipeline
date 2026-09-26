import type { ViewCtx } from "../../context";
import { resolvedFromLabel, versionRefLabel, type Resolved } from "../../dag/resolve";
import { formatRelative, formatStamp, formatTime } from "../../format";
import type { TaskRef } from "../../types";
import { Card, KVRows, Link, linkClass, tableClass, tbodyClass, tdClass, thClass, theadClass } from "../../ui/primitives";
import type { PipelineData } from "./data";

// S2e — the Configuration tab (docs/decisions/0016 §4, Figma screen 6): flat and read-only, a
// "what is this thing" view kept separate from anything editable.

const stamp = (iso: string) => <span title={`${formatTime(iso)} (${formatRelative(iso)})`}>{formatStamp(iso)}</span>;

export function ConfigTab({ v, d, refs, resolved }: { v: ViewCtx; d: PipelineData; refs: TaskRef[]; resolved: Resolved }) {
  const p = d.pipeline;
  return (
    <div className="flex max-w-4xl flex-col gap-4">
      <Card title="General">
        <KVRows
          rows={[
            { label: "Pipeline ID", value: p.id, mono: true },
            { label: "Name", value: p.name },
            { label: "Description", value: p.description || "—" },
            { label: "Owner", value: p.createdBy },
            { label: "Created", value: stamp(p.createdAt), mono: true },
            { label: "Updated", value: stamp(p.updatedAt), mono: true },
          ]}
        />
      </Card>
      <Card title="Versions">
        <KVRows
          rows={[
            { label: "Latest saved version", value: d.latest ? <>v{d.latest.version} · {stamp(d.latest.createdAt)}</> : "None yet", mono: true },
            {
              label: "Draft",
              value: !d.draft ? "None" : d.draftDiffers ? <>Differs from the latest version · saved {stamp(d.draft.updatedAt)} by {d.draft.updatedBy}</> : "Same as the latest version",
            },
            { label: "Runs use", value: p.pinnedVersion ? `v${p.pinnedVersion} (pinned)` : "The latest saved version" },
          ]}
        />
      </Card>
      <Card title="Execution">
        <KVRows
          rows={[
            { label: "Concurrent runs", value: p.allowConcurrentRuns ? "Allowed" : "Not allowed" },
            { label: "Platform access cap", value: p.roleCeiling },
          ]}
        />
      </Card>
      <Card title={`Tasks (${d.viewed ? `v${d.viewed.version}` : "current"})`}>
        {refs.length === 0 ? (
          <p className="px-4 py-3 text-sm text-slate-500 dark:text-slate-400">No tasks yet.</p>
        ) : (
          <div className="relative overflow-x-auto">
            <table className={tableClass}>
              <thead className={theadClass}>
                <tr>
                  <th className={thClass}>Key</th>
                  <th className={thClass}>Task</th>
                  <th className={thClass}>Version ref</th>
                  <th className={thClass}>Depends on</th>
                </tr>
              </thead>
              <tbody className={tbodyClass}>
                {refs.map((r) => {
                  const node = resolved[r.key];
                  return (
                    <tr key={r.key}>
                      <td className={`${tdClass} font-mono text-xs`}>{r.key}</td>
                      <td className={tdClass}>
                        {node?.task ? (
                          <Link href={v.href({ name: "task", id: node.task.id })} onNavigate={v.goPath} className={linkClass}>
                            {node.task.name}
                          </Link>
                        ) : (
                          <span className="text-slate-400">{node?.error ?? "…"}</span>
                        )}
                      </td>
                      <td className={`${tdClass} font-mono text-xs`}>
                        {versionRefLabel(r)}
                        {r.taskVersion === "latest" && node && <span className="text-slate-500"> → {resolvedFromLabel(node.from)}</span>}
                      </td>
                      <td className={`${tdClass} font-mono text-xs`}>{r.dependsOn.join(", ") || "—"}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
