import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { ViewCtx } from "../context";
import { errorMessage } from "../hooks";
import { suggestProject } from "../projects";
import type { Project } from "../types";
import { Banner, Button, Card, tableClass, tbodyClass, tdClass, thClass, theadClass } from "../ui/primitives";

// ADR 0124's "suggest from names", for pipelines and tasks (not a bindable kind, so per-resource
// assignment): a project is proposed when its slug is a "-"-boundary prefix of the name, and nothing is
// assigned until the person confirms it, one row at a time or for the rows they tick.

interface Row {
  kind: "pipeline" | "task";
  id: string;
  name: string;
  description: string;
  project: Project;
}

export function SuggestProjects({ v, projects, onDone }: { v: ViewCtx; projects: Project[]; onDone: (changed: boolean) => void }) {
  const [rows, setRows] = useState<Row[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [done, setDone] = useState<Set<string>>(new Set());
  const [failed, setFailed] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let alive = true;
    Promise.all([api.listPipelines(v.api), api.listTasks(v.api)]).then(
      ([ps, ts]) => {
        if (!alive) return;
        const found: Row[] = [];
        for (const [kind, items] of [["pipeline", ps.items], ["task", ts.items]] as const) {
          for (const item of items) {
            if (item.project) continue; // only unassigned items: a suggestion never moves one
            const project = suggestProject(item.name, projects);
            if (project) found.push({ kind, id: item.id, name: item.name, description: item.description, project });
          }
        }
        setRows(found);
      },
      (err: unknown) => alive && setLoadError(errorMessage(err)),
    );
    return () => {
      alive = false;
    };
  }, [v.api, projects]);

  const key = (r: Row) => `${r.kind}:${r.id}`;

  async function assign(targets: Row[]) {
    setBusy(true);
    for (const r of targets) {
      try {
        if (r.kind === "pipeline") await api.updatePipeline(v.api, r.id, r.name, r.description, r.project.slug);
        else await api.updateTask(v.api, r.id, r.name, r.description, r.project.slug);
        setDone((was) => new Set(was).add(key(r)));
        setPicked((was) => {
          const next = new Set(was);
          next.delete(key(r));
          return next;
        });
      } catch (err) {
        setFailed((was) => ({ ...was, [key(r)]: errorMessage(err) }));
      }
    }
    setBusy(false);
  }

  const open = (rows ?? []).filter((r) => !done.has(key(r)));
  const toggle = (r: Row) =>
    setPicked((was) => {
      const next = new Set(was);
      if (next.has(key(r))) next.delete(key(r));
      else next.add(key(r));
      return next;
    });

  return (
    <Card
      title="Suggest projects from names"
      label="Suggest projects from names"
      actions={
        <Button size="sm" onClick={() => onDone(done.size > 0)} disabled={busy}>
          Close
        </Button>
      }
    >
      <div className="flex flex-col gap-3 p-4">
        <p className="text-xs text-slate-500 dark:text-slate-400">
          For pipelines and tasks with no project yet, whose name starts with a project's slug (the longest match wins). Nothing changes until you assign it.
        </p>
        {loadError && <Banner tone="error">Could not list pipelines and tasks: {loadError}</Banner>}
        {rows && open.length === 0 && <p className="text-sm text-slate-600 dark:text-slate-300">{done.size ? `Assigned ${done.size}. Nothing else to suggest.` : "Nothing to suggest: no unassigned name starts with a project's slug."}</p>}
        {open.length > 0 && (
          <>
            <div className="flex flex-wrap items-center gap-2">
              <Button size="sm" variant="primary" disabled={busy || picked.size === 0} onClick={() => assign(open.filter((r) => picked.has(key(r))))}>
                {busy ? "Assigning…" : `Assign the ${picked.size} ticked`}
              </Button>
              <Button size="sm" disabled={busy} onClick={() => setPicked(new Set(open.map(key)))}>
                Tick all {open.length}
              </Button>
              {done.size > 0 && <span className="text-xs text-emerald-700 dark:text-emerald-400">Assigned {done.size}.</span>}
            </div>
            <table className={tableClass} aria-label="Suggested projects">
              <thead className={theadClass}>
                <tr>
                  <th className={thClass}>
                    <span className="sr-only">Assign</span>
                  </th>
                  <th className={thClass}>Name</th>
                  <th className={thClass}>Kind</th>
                  <th className={thClass}>Suggested project</th>
                  <th className={thClass}>
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody className={tbodyClass}>
                {open.map((r) => (
                  <tr key={key(r)}>
                    <td className={tdClass}>
                      <input type="checkbox" aria-label={`Assign ${r.name} to ${r.project.name}`} checked={picked.has(key(r))} onChange={() => toggle(r)} disabled={busy} />
                    </td>
                    <td className={`${tdClass} font-mono text-xs`}>{r.name}</td>
                    <td className={tdClass}>{r.kind}</td>
                    <td className={tdClass}>
                      {r.project.name} <span className="font-mono text-xs text-slate-500 dark:text-slate-400">{r.project.slug}</span>
                      {failed[key(r)] && <p className="text-xs text-red-600 dark:text-red-400">{failed[key(r)]}</p>}
                    </td>
                    <td className={`${tdClass} text-right`}>
                      <Button size="sm" disabled={busy} onClick={() => assign([r])} aria-label={`Assign ${r.name} now`}>
                        Assign
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
      </div>
    </Card>
  );
}
