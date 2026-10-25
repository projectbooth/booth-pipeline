import type { ApiContext } from "../api/client";
import { orderedProjects, projectName, useProjects } from "../projects";
import type { Project } from "../types";
import { Badge, Field, inputClass } from "../ui/primitives";

/** The Project field on a pipeline's or task's create/edit form (ADR 0124). Children are indented under
 *  their parents. Archived projects aren't offered, but an item already in one keeps it, shown as such. */
export function ProjectSelect({
  id,
  value,
  onChange,
  projects,
  error,
}: {
  id: string;
  value: string | null;
  onChange: (slug: string | null) => void;
  projects: Project[];
  error?: string;
}) {
  const offered = orderedProjects(projects).filter(({ project }) => !project.archived || project.slug === value);
  const unknown = value !== null && !projects.some((p) => p.slug === value);
  return (
    <Field id={id} label="Project" error={error} help="For grouping and finding things only: a project doesn't change who can see or run anything.">
      {(fp) => (
        <select {...fp} className={inputClass} value={value ?? ""} onChange={(e) => onChange(e.target.value === "" ? null : e.target.value)}>
          <option value="">No project</option>
          {offered.map(({ project, depth }) => (
            <option key={project.slug} value={project.slug}>
              {depth > 0 ? "    " : ""}
              {project.name}
              {project.archived ? " (archived)" : ""}
            </option>
          ))}
          {unknown && <option value={value!}>{value} (not in the registry)</option>}
        </select>
      )}
    </Field>
  );
}

/** An item's project in its page header, when it has one and booth-core's registry answers. */
export function ProjectBadge({ api, slug }: { api: ApiContext; slug: string | null | undefined }) {
  const state = useProjects(api);
  if (!slug || state.status !== "ready") return null;
  const project = state.projects.find((p) => p.slug === slug);
  return (
    <Badge tone="slate" title="Project: for grouping and finding things only">
      {projectName(state.projects, slug)}
      {project?.archived ? " (archived)" : ""}
    </Badge>
  );
}

