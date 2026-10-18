import type { TaskOutput as Kept, TaskRun } from "../../types";

// What one task returned (GET /runs/{id}'s `output`), for editors and owners only: like a Spark driver
// log, an output can carry whatever its code put in it, so the API leaves it out for viewers.

const KEPT_LIMIT_KB = 64;

function kilobytes(bytes: number): string {
  return `${Math.ceil(bytes / 1024)} KB`;
}

export function TaskOutputPanel({ task }: { task: TaskRun }) {
  return (
    <section aria-label={`Output of ${task.taskKey}`} className="flex flex-col gap-2">
      <h3 className="text-sm font-semibold text-slate-800 dark:text-slate-100">
        Output — <span className="font-mono">{task.taskKey}</span>
      </h3>
      <OutputBody task={task} output={task.output ?? null} />
    </section>
  );
}

function Note({ children }: { children: React.ReactNode }) {
  return <p className="text-xs text-slate-600 dark:text-slate-400">{children}</p>;
}

function OutputBody({ task, output }: { task: TaskRun; output: Kept | null }) {
  if (output === null) {
    if (["pending", "running", "retrying"].includes(task.status)) return <Note>Shown once the task succeeds.</Note>;
    if (task.status === "succeeded") return <Note>No output was kept for this task: it ran before booth-pipeline kept outputs.</Note>;
    return <Note>No output: the task didn&apos;t succeed.</Note>;
  }
  if ("omitted" in output) {
    return (
      <Note>
        {output.omitted === "tooLarge"
          ? `Not kept: the output is ${kilobytes(output.bytes)}, more than the ${KEPT_LIMIT_KB} KB a run keeps to show.`
          : "Not kept: the output contains a NUL character, which can't be stored."}{" "}
        Its downstream tasks still received it in full.
      </Note>
    );
  }
  if (output.value === null) return <Note>The task returned nothing (null).</Note>;
  return (
    <pre
      aria-label={`Returned value of ${task.taskKey}`}
      className="max-h-80 overflow-auto whitespace-pre-wrap break-words rounded-lg border border-slate-200 bg-slate-50 p-3 font-mono text-xs leading-relaxed text-slate-800 dark:border-slate-700 dark:bg-slate-950 dark:text-slate-200"
    >
      {JSON.stringify(output.value, null, 2)}
    </pre>
  );
}
