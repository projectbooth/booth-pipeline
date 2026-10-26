import { useState, type FormEvent } from "react";
import { api } from "../api/client";
import type { ViewCtx } from "../context";
import { formatRelative, formatStamp, formatTime } from "../format";
import { errorMessage, useLoad } from "../hooks";
import type { TaskSecret } from "../types";
import {
  Banner,
  Button,
  Card,
  EmptyState,
  Field,
  InlineConfirm,
  Loaded,
  PageHeader,
  inputClass,
  monoClass,
  tableClass,
  tbodyClass,
  tdClass,
  thClass,
  theadClass,
} from "../ui/primitives";

// ADR 0128: the workspace's task secrets. Owners set, replace and delete them; editors see the names, to
// declare them on tasks. A value is sent once and never shown again, by anyone.

const NAME_RE = /^[A-Za-z][A-Za-z0-9_-]{0,63}$/;
const MIN_LENGTH = 8;

export function SecretsPage({ v }: { v: ViewCtx }) {
  const isOwner = v.role === "owner";
  const load = useLoad(() => api.listSecrets(v.api), [v.api]);
  const [editing, setEditing] = useState<string | null>(null); // a name being replaced, or "" for a new one
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  async function remove(name: string) {
    setBusy(true);
    setActionError(null);
    try {
      await api.deleteSecret(v.api, name);
      setConfirmDelete(null);
      load.reload();
    } catch (err) {
      setActionError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Secrets"
        subtitle="Outside keys, such as an API key, that a task is given as an environment variable. Only the tasks that declare a secret receive it."
        actions={
          isOwner &&
          editing === null && (
            <Button variant="primary" onClick={() => setEditing("")}>
              New secret
            </Button>
          )
        }
      />
      <Banner tone="info">
        A value is never shown again once saved, and is taken out of logs, errors and outputs as <code>[secret NAME]</code>. That can't catch a
        value split across two log lines, a transformed value (base64, URL-encoded), or anything a task sends over the network: whoever can edit a
        task that declares a secret can read it. Spark tasks can't use secrets.
      </Banner>
      {isOwner && editing !== null && (
        <SecretForm
          v={v}
          name={editing}
          onDone={(saved) => {
            setEditing(null);
            if (saved) load.reload();
          }}
        />
      )}
      {actionError && <Banner tone="error">{actionError}</Banner>}
      <Loaded state={load.state} retry={load.reload}>
        {(secrets) =>
          secrets.length === 0 ? (
            <EmptyState title="No secrets yet">{isOwner ? "Add one with New secret, then declare it on a task." : "A workspace owner adds them."}</EmptyState>
          ) : (
            <div className="relative overflow-x-auto">
              <table className={tableClass} aria-label="Secrets">
                <thead className={theadClass}>
                  <tr>
                    <th className={thClass}>Name</th>
                    <th className={thClass}>Last changed</th>
                    <th className={thClass}>By</th>
                    {isOwner && (
                      <th className={thClass}>
                        <span className="sr-only">Actions</span>
                      </th>
                    )}
                  </tr>
                </thead>
                <tbody className={tbodyClass}>
                  {secrets.map((s) => (
                    <SecretRow
                      key={s.name}
                      s={s}
                      isOwner={isOwner}
                      busy={busy}
                      confirming={confirmDelete === s.name}
                      onReplace={() => setEditing(s.name)}
                      onDelete={() => setConfirmDelete(s.name)}
                      onConfirm={() => void remove(s.name)}
                      onCancel={() => setConfirmDelete(null)}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          )
        }
      </Loaded>
      {isOwner && (
        <p className="text-xs text-slate-500 dark:text-slate-400">
          Values are encrypted with a key your operator holds outside the database. If that key is lost, every secret here becomes unreadable and
          has to be entered again.
        </p>
      )}
    </div>
  );
}

function SecretRow({
  s,
  isOwner,
  busy,
  confirming,
  onReplace,
  onDelete,
  onConfirm,
  onCancel,
}: {
  s: TaskSecret;
  isOwner: boolean;
  busy: boolean;
  confirming: boolean;
  onReplace: () => void;
  onDelete: () => void;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <>
      <tr>
        <td className={`${tdClass} ${monoClass}`}>{s.name}</td>
        <td className={tdClass} title={formatTime(s.updatedAt)}>
          {formatStamp(s.updatedAt)} <span className="text-xs text-slate-500 dark:text-slate-400">({formatRelative(s.updatedAt)})</span>
        </td>
        <td className={tdClass}>{s.updatedBy || "—"}</td>
        {isOwner && (
          <td className={`${tdClass} text-right`}>
            <div className="flex justify-end gap-2">
              <Button size="sm" onClick={onReplace} aria-label={`Replace ${s.name}`}>
                Replace
              </Button>
              <Button size="sm" variant="danger" onClick={onDelete} aria-label={`Delete ${s.name}`}>
                Delete
              </Button>
            </div>
          </td>
        )}
      </tr>
      {confirming && (
        <tr>
          <td colSpan={4} className={tdClass}>
            <InlineConfirm
              message={<>Delete {s.name}? Tasks that declare it fail until it is set again.</>}
              confirmLabel="Delete"
              busyLabel="Deleting…"
              busy={busy}
              onConfirm={onConfirm}
              onCancel={onCancel}
            />
          </td>
        </tr>
      )}
    </>
  );
}

function SecretForm({ v, name: existing, onDone }: { v: ViewCtx; name: string; onDone: (saved: boolean) => void }) {
  const replacing = existing !== "";
  const [name, setName] = useState(existing);
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ message: string; field?: string } | null>(null);

  const nameProblem = name && !NAME_RE.test(name) ? "A letter, then up to 63 letters, digits, _ or -." : undefined;
  const valueProblem = value && value.length < MIN_LENGTH ? `At least ${MIN_LENGTH} characters: a shorter value can't be taken out of logs safely.` : undefined;

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!name || !value || nameProblem || valueProblem) return;
    setBusy(true);
    setError(null);
    try {
      await api.setSecret(v.api, name, value);
      setValue("");
      onDone(true);
    } catch (err) {
      const field = (err as { field?: string }).field;
      setError({ message: errorMessage(err), field: field ?? undefined });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card title={replacing ? `Replace ${existing}` : "New secret"} label={replacing ? `Replace ${existing}` : "New secret"}>
      <form onSubmit={submit} className="flex flex-col gap-3 p-4" autoComplete="off">
        {error && !error.field && <Banner tone="error">{error.message}</Banner>}
        <Field id="secret-name" label="Name" error={nameProblem ?? (error?.field === "name" ? error.message : undefined)} help="What tasks declare, e.g. FMP.">
          {(p) => <input {...p} className={`${inputClass} ${monoClass}`} value={name} disabled={replacing} onChange={(e) => setName(e.target.value)} spellCheck={false} />}
        </Field>
        <Field
          id="secret-value"
          label={replacing ? "New value" : "Value"}
          error={valueProblem ?? (error?.field === "value" ? error.message : undefined)}
          help={replacing ? "Replaces the stored value. Running tasks keep the old one; their next attempt gets the new one." : "Never shown again once saved."}
        >
          {(p) => (
            <input {...p} type="password" autoComplete="new-password" className={`${inputClass} ${monoClass}`} value={value} onChange={(e) => setValue(e.target.value)} spellCheck={false} />
          )}
        </Field>
        <div className="flex gap-2">
          <Button type="submit" variant="primary" disabled={busy || !name || !value || !!nameProblem || !!valueProblem}>
            {busy ? "Saving…" : replacing ? "Replace" : "Save"}
          </Button>
          <Button onClick={() => onDone(false)} disabled={busy}>
            Cancel
          </Button>
        </div>
      </form>
    </Card>
  );
}
