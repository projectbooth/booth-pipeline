import { useEffect, useState } from "react";
import { api, type ApiContext } from "../api/client";
import { errorMessage } from "../hooks";
import type { TaskSecretRef } from "../types";
import { Button, inputClass, monoClass } from "../ui/primitives";

// ADR 0128: the workspace task secrets this task is given, each as an environment variable in its own
// process. The names come from the workspace's Secrets page; the values never reach this form.

/** A server error about secrets: the list ("secrets") or one entry ("secrets[1].env"). */
export function isSecretsField(field: string | null | undefined): boolean {
  return field === "secrets" || /^secrets\[\d+\]\.(name|env)$/.test(field ?? "");
}

/** A default environment variable for a secret name: FMP -> FMP, fmp-backup -> FMP_BACKUP. */
export function envFor(name: string): string {
  const env = name.toUpperCase().replace(/[^A-Z0-9_]/g, "_");
  return /^[0-9]/.test(env) ? `_${env}` : env;
}

export interface SecretsFieldProps {
  api: ApiContext;
  value: TaskSecretRef[];
  error?: { message: string; field?: string | null };
  onChange: (secrets: TaskSecretRef[]) => void;
}

export function SecretsField({ api: ctx, value, error, onChange }: SecretsFieldProps) {
  const [names, setNames] = useState<string[] | null>(null);
  const [namesError, setNamesError] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    api.listSecrets(ctx).then(
      (items) => alive && setNames(items.map((s) => s.name)),
      (err: unknown) => alive && setNamesError(errorMessage(err)),
    );
    return () => {
      alive = false;
    };
  }, [ctx]);

  const unused = (names ?? []).filter((n) => !value.some((s) => s.name === n));
  const rowError = (i: number, part: "name" | "env") => (error?.field === `secrets[${i}].${part}` ? error.message : undefined);
  const set = (i: number, next: TaskSecretRef) => onChange(value.map((s, j) => (j === i ? next : s)));

  return (
    <section aria-label="Secrets" className="flex flex-col gap-1.5">
      <h3 className="text-xs font-medium text-slate-600 dark:text-slate-300">Secrets</h3>
      <p className="text-xs text-slate-500 dark:text-slate-400">
        Workspace secrets this task gets as environment variables, read with <code>os.environ[&quot;NAME&quot;]</code>. Values are set by a
        workspace owner on the Secrets page and taken out of this task's logs, errors and output; anyone who can edit this task can still read them.
      </p>
      {value.length > 0 && (
        <ul aria-label="Secrets this task gets" className="flex flex-col gap-2">
          {value.map((s, i) => (
            <li key={i} className="flex flex-col gap-1">
              <div className="flex flex-wrap items-center gap-2">
                <label className="sr-only" htmlFor={`secret-${i}-name`}>
                  Secret
                </label>
                {names && (names.includes(s.name) || s.name === "") ? (
                  <select
                    id={`secret-${i}-name`}
                    className={`${inputClass} ${monoClass} w-auto`}
                    value={s.name}
                    onChange={(e) => set(i, { name: e.target.value, env: s.env || envFor(e.target.value) })}
                  >
                    <option value="">Choose a secret</option>
                    {[s.name, ...unused].filter(Boolean).map((n) => (
                      <option key={n} value={n}>
                        {n}
                      </option>
                    ))}
                  </select>
                ) : (
                  <input
                    id={`secret-${i}-name`}
                    className={`${inputClass} ${monoClass} w-48`}
                    value={s.name}
                    placeholder="Secret name"
                    spellCheck={false}
                    onChange={(e) => set(i, { ...s, name: e.target.value })}
                  />
                )}
                <span className="text-xs text-slate-500 dark:text-slate-400">as</span>
                <label className="sr-only" htmlFor={`secret-${i}-env`}>
                  Environment variable for {s.name || "this secret"}
                </label>
                <input
                  id={`secret-${i}-env`}
                  className={`${inputClass} ${monoClass} w-56`}
                  value={s.env}
                  placeholder="ENV_VAR"
                  spellCheck={false}
                  onChange={(e) => set(i, { ...s, env: e.target.value })}
                />
                <Button size="sm" variant="ghost" aria-label={`Remove ${s.name || "secret"}`} onClick={() => onChange(value.filter((_, j) => j !== i))}>
                  Remove
                </Button>
                {names && s.name && !names.includes(s.name) && (
                  <span className="text-xs text-amber-700 dark:text-amber-400">Not set in this workspace: the task fails until an owner sets it.</span>
                )}
              </div>
              {(rowError(i, "name") ?? rowError(i, "env")) && (
                <p role="alert" className="text-xs text-red-600 dark:text-red-400">
                  {rowError(i, "name") ?? rowError(i, "env")}
                </p>
              )}
            </li>
          ))}
        </ul>
      )}
      {error?.field === "secrets" && (
        <p role="alert" className="text-xs text-red-600 dark:text-red-400">
          {error.message}
        </p>
      )}
      {namesError && <p className="text-xs text-slate-500 dark:text-slate-400">The workspace's secrets couldn't be listed ({namesError}); type the name.</p>}
      <div>
        <Button size="sm" onClick={() => onChange([...value, { name: unused.length === 1 ? unused[0] : "", env: unused.length === 1 ? envFor(unused[0]) : "" }])}>
          Add a secret
        </Button>
      </div>
    </section>
  );
}
