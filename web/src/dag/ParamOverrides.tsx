import { useEffect, useRef, useState } from "react";
import { Field, inputClass, monoClass } from "../ui/primitives";

// ADR 0078: one DAG node's own param overrides, merged over the referenced task's saved params
// when this node runs — shallow, key by key, the node's value replacing the task's. Saved with the
// pipeline (draft or version), never on the task: the task's own params stay its defaults, which
// every node without an override (and every other pipeline) keeps using.

/** Exactly the server's rule (resolve.py `node_config`), so the preview can't disagree with a run. */
export function effectiveParams(base: Record<string, unknown>, overrides: Record<string, unknown> | undefined): Record<string, unknown> {
  return { ...base, ...(overrides ?? {}) };
}

/** Short on-canvas label for a node's overrides — what tells four `fetch_quote` nodes apart. */
export function overridesLabel(overrides: Record<string, unknown> | undefined): string | null {
  const entries = Object.entries(overrides ?? {});
  if (entries.length === 0) return null;
  const [k, val] = entries[0];
  const first = `${k}=${typeof val === "string" ? val : JSON.stringify(val)}`;
  return entries.length > 1 ? `${first} +${entries.length - 1}` : first;
}

const pretty = (o: Record<string, unknown>) => JSON.stringify(o, null, 2);

export function ParamOverrides({
  overrides,
  baseParams,
  readOnly,
  error,
  onChange,
}: {
  overrides: Record<string, unknown> | undefined;
  /** The referenced task's own params (its defaults), or null while it is still loading. */
  baseParams: Record<string, unknown> | null;
  readOnly: boolean;
  /** A server message about this node's overrides (field `paramOverrides`). */
  error?: string;
  onChange: (next: Record<string, unknown>) => void;
}) {
  const committed = overrides ?? {};
  const [text, setText] = useState(() => (Object.keys(committed).length ? pretty(committed) : ""));
  const [parseError, setParseError] = useState<string | null>(null);
  const editing = useRef(false);

  // Follow changes made elsewhere (a discard, a reload, re-selecting) — never while typing.
  const committedJson = JSON.stringify(committed);
  useEffect(() => {
    if (editing.current) return;
    const current = JSON.parse(committedJson) as Record<string, unknown>;
    setText(Object.keys(current).length ? pretty(current) : "");
    setParseError(null);
  }, [committedJson]);

  function edit(next: string) {
    setText(next);
    try {
      const parsed: unknown = next.trim() === "" ? {} : JSON.parse(next);
      if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("overrides must be a JSON object, like {\"symbol\": \"HOOD\"}");
      setParseError(null);
      onChange(parsed as Record<string, unknown>);
    } catch (e) {
      setParseError(e instanceof Error ? e.message : "invalid JSON");
    }
  }

  const effective = baseParams ? effectiveParams(baseParams, committed) : null;
  const overridden = new Set(Object.keys(committed));

  return (
    <div className="flex flex-col gap-3">
      <p className="text-xs text-slate-500 dark:text-slate-400">
        Values for this node only, merged over the task's own params when it runs (key by key: a key set here replaces the task's). Saved with the pipeline; the task itself is not changed.
      </p>
      <div className="grid gap-4 md:grid-cols-3">
        <div className="flex min-w-0 flex-col gap-1">
          <span className="text-xs font-medium text-slate-600 dark:text-slate-300">Task's own params (defaults)</span>
          <pre aria-label="Task's own params" className={`${monoClass} max-h-48 overflow-auto rounded-md border border-slate-200 bg-slate-50 p-2 dark:border-slate-700 dark:bg-slate-950`}>
            {baseParams === null ? "…" : Object.keys(baseParams).length ? pretty(baseParams) : "{}"}
          </pre>
        </div>
        <Field id="node-param-overrides" label="This node's overrides (JSON)" error={parseError ?? error}>
          {(p) => (
            <textarea
              {...p}
              readOnly={readOnly}
              spellCheck={false}
              placeholder={readOnly ? "none" : '{"symbol": "HOOD"}'}
              className={`${inputClass} ${monoClass} h-36 resize-y`}
              value={text}
              onFocus={() => {
                editing.current = true;
              }}
              onBlur={() => {
                editing.current = false;
                if (!parseError) setText(Object.keys(committed).length ? pretty(committed) : "");
              }}
              onChange={(e) => edit(e.target.value)}
            />
          )}
        </Field>
        <div className="flex min-w-0 flex-col gap-1">
          <span className="text-xs font-medium text-slate-600 dark:text-slate-300">Runs with</span>
          <pre aria-label="Effective params" className={`${monoClass} max-h-48 overflow-auto rounded-md border border-slate-200 bg-slate-50 p-2 dark:border-slate-700 dark:bg-slate-950`}>
            {effective === null
              ? "…"
              : Object.keys(effective).length === 0
                ? "{}"
                : Object.entries(effective).map(([k, val]) => (
                    <div key={k} className={overridden.has(k) ? "font-semibold text-indigo-700 dark:text-indigo-300" : ""}>
                      {k}: {JSON.stringify(val)}
                      {overridden.has(k) && <span className="font-normal text-slate-500"> (this node)</span>}
                    </div>
                  ))}
          </pre>
        </div>
      </div>
    </div>
  );
}
