import { useState } from "react";
import type { PipStatus } from "../types";
import { Banner, Field, inputClass, monoClass } from "../ui/primitives";

// ADR 0115: the Python packages a task installs into its own pod before its code runs. One
// requirement per line, from the install's package index only: the server refuses pip options,
// URLs and paths, naming the entry. Blank lines and `#` comments are the editor's convenience and are
// dropped before saving, so what is stored is exactly the list pip receives.

/** The lines of the textarea as the list the server stores. */
export function parseRequirements(text: string): string[] {
  return text
    .split(/\r?\n/)
    .map((l) => l.trim())
    .filter((l) => l !== "" && !l.startsWith("#"));
}

/** A server error about requirements: the list as a whole ("requirements") or one entry
 *  ("requirements[3]"). */
export function isRequirementsField(field: string | null | undefined): boolean {
  return field === "requirements" || /^requirements\[\d+\]$/.test(field ?? "");
}

export interface RequirementsFieldProps {
  value: string[];
  /** Whether this install can install packages at all (GET /api/config). Unknown: no warning. */
  pip?: PipStatus | null;
  error?: string;
  onChange: (requirements: string[]) => void;
}

export function RequirementsField({ value, pip, error, onChange }: RequirementsFieldProps) {
  // The text as typed (comments, blank lines and all); the parsed list is what the config holds.
  const [draft, setDraft] = useState(() => value.join("\n"));
  const text = sameList(parseRequirements(draft), value) ? draft : value.join("\n");
  return (
    <section aria-label="Packages" className="flex flex-col gap-1.5">
      <Field
        id="task-requirements"
        label="Python packages"
        error={error}
        help={
          <>
            One requirement per line, e.g. <code>requests==2.32.3</code>. Installed from this install's package index into the task's own pod,
            before its code runs, on every run. No URLs, paths or pip options.
          </>
        }
      >
        {(p) => (
          <textarea
            {...p}
            className={`${inputClass} ${monoClass} h-24 resize-y`}
            spellCheck={false}
            placeholder="requests==2.32.3"
            value={text}
            onChange={(e) => {
              setDraft(e.target.value);
              onChange(parseRequirements(e.target.value));
            }}
          />
        )}
      </Field>
      {pip && !pip.available && (
        <Banner tone="warn">
          This install can't install packages: {pip.reason ?? "no package index is configured"}. A task with packages fails to start until your
          operator sets this up.
        </Banner>
      )}
    </section>
  );
}

function sameList(a: string[], b: string[]): boolean {
  return a.length === b.length && a.every((x, i) => x === b[i]);
}
