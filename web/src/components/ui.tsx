import type { AnchorHTMLAttributes, ButtonHTMLAttributes, MouseEvent, ReactNode } from "react";
import type { LoadState } from "../hooks";
import type { RunStatus, TaskStatus } from "../types";

// Small presentational primitives, styled with the same Tailwind slate/indigo palette
// booth-catalog, booth-storage and booth-module-store use, so the native modules sit together
// consistently in the shell. booth-design's own component library is not published as a package
// yet (its package.json is `private`), so nothing here can import it — see docs/decisions/0004.
// Every colour has a `dark:` counterpart: booth-design toggles dark mode via a data-theme
// attribute, which tailwind.config.js's darkMode selector follows.

type Variant = "primary" | "secondary" | "danger";

const VARIANTS: Record<Variant, string> = {
  primary: "bg-indigo-600 text-white hover:bg-indigo-500 disabled:opacity-50",
  secondary:
    "border border-slate-300 text-slate-700 hover:bg-slate-100 disabled:opacity-50 dark:border-slate-600 dark:text-slate-300 dark:hover:bg-slate-800",
  danger:
    "border border-red-300 text-red-700 hover:bg-red-50 disabled:opacity-50 dark:border-red-800 dark:text-red-400 dark:hover:bg-red-950",
};

export function Button({
  variant = "secondary",
  className = "",
  type = "button",
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant }) {
  return (
    <button
      type={type}
      className={`rounded-md px-3 py-1.5 text-sm font-medium focus:outline-none focus:ring-2 focus:ring-indigo-500 ${VARIANTS[variant]} ${className}`}
      {...rest}
    />
  );
}

export const inputClass =
  "w-full rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm text-slate-900 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 disabled:opacity-60 dark:border-slate-600 dark:bg-slate-900 dark:text-slate-100";

export const monoClass = "font-mono text-xs leading-relaxed";

/** A labelled form control with optional help and error text, wired for accessibility: the label
 *  targets the control, and help/error are linked via aria-describedby. */
export function Field({
  id,
  label,
  help,
  error,
  required,
  children,
}: {
  id: string;
  label: string;
  help?: string;
  error?: string;
  required?: boolean;
  children: (props: { id: string; "aria-describedby"?: string; "aria-invalid"?: boolean }) => ReactNode;
}) {
  const describedBy = [help ? `${id}-help` : "", error ? `${id}-error` : ""].filter(Boolean).join(" ") || undefined;
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="text-xs font-medium text-slate-600 dark:text-slate-300">
        {label}
        {required && (
          <span className="ml-0.5 text-red-500" aria-hidden="true">
            *
          </span>
        )}
      </label>
      {children({ id, "aria-describedby": describedBy, "aria-invalid": error ? true : undefined })}
      {help && (
        <p id={`${id}-help`} className="text-xs text-slate-500 dark:text-slate-400">
          {help}
        </p>
      )}
      {error && (
        <p id={`${id}-error`} role="alert" className="text-xs text-red-600 dark:text-red-400">
          {error}
        </p>
      )}
    </div>
  );
}

export function Banner({ tone, children }: { tone: "error" | "info" | "success" | "warn"; children: ReactNode }) {
  const tones = {
    error: "border-red-200 bg-red-50 text-red-800 dark:border-red-900 dark:bg-red-950 dark:text-red-200",
    info: "border-slate-200 bg-slate-50 text-slate-700 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300",
    success: "border-emerald-200 bg-emerald-50 text-emerald-800 dark:border-emerald-900 dark:bg-emerald-950 dark:text-emerald-200",
    warn: "border-amber-200 bg-amber-50 text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-200",
  };
  return (
    <div role={tone === "error" ? "alert" : "status"} className={`rounded-md border px-3 py-2 text-sm ${tones[tone]}`}>
      {children}
    </div>
  );
}

export function PageHeader({ title, subtitle, actions }: { title: string; subtitle?: string; actions?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div className="min-w-0">
        <h2 className="break-words text-lg font-semibold text-slate-900 dark:text-slate-100">{title}</h2>
        {subtitle && <p className="mt-0.5 text-sm text-slate-500 dark:text-slate-400">{subtitle}</p>}
      </div>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </div>
  );
}

export function Chip({ children, tone = "slate" }: { children: ReactNode; tone?: "slate" | "indigo" | "amber" | "emerald" | "sky" | "red" }) {
  const tones = {
    slate: "bg-slate-200 text-slate-700 dark:bg-slate-800 dark:text-slate-300",
    indigo: "bg-indigo-100 text-indigo-800 dark:bg-indigo-950 dark:text-indigo-300",
    amber: "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300",
    emerald: "bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300",
    sky: "bg-sky-100 text-sky-800 dark:bg-sky-950 dark:text-sky-300",
    red: "bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-300",
  };
  return <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${tones[tone]}`}>{children}</span>;
}

type Tone = "slate" | "indigo" | "amber" | "emerald" | "sky" | "red";
const STATUS_TONE: Record<RunStatus | TaskStatus, Tone> = {
  queued: "slate",
  pending: "slate",
  running: "sky",
  retrying: "amber",
  succeeded: "emerald",
  failed: "red",
  skipped: "slate",
  canceled: "amber",
};

/** Status is never conveyed by colour alone: the word is always shown. */
export function StatusChip({ status }: { status: RunStatus | TaskStatus }) {
  return <Chip tone={STATUS_TONE[status]}>{status}</Chip>;
}

/** A link that navigates in-app without a page reload (see navigation.ts's defaultNavigate),
 *  while still being a real anchor: middle-click, ctrl/cmd-click and "open in new tab" work. */
export function Link({
  href,
  onNavigate,
  onClick,
  ...rest
}: AnchorHTMLAttributes<HTMLAnchorElement> & { href: string; onNavigate: (path: string) => void }) {
  return (
    <a
      href={href}
      onClick={(ev: MouseEvent<HTMLAnchorElement>) => {
        onClick?.(ev);
        if (ev.defaultPrevented || ev.button !== 0 || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return;
        ev.preventDefault();
        onNavigate(href);
      }}
      {...rest}
    />
  );
}

export const linkClass = "text-indigo-600 hover:underline dark:text-indigo-400";

export function Spinner({ label = "Loading…" }: { label?: string }) {
  return (
    <p role="status" className="text-sm text-slate-500 dark:text-slate-400">
      {label}
    </p>
  );
}

/** Renders a `useLoad` state: spinner, error banner (with retry), or the loaded content. */
export function Loaded<T>({ state, retry, children }: { state: LoadState<T>; retry?: () => void; children: (data: T) => ReactNode }) {
  if (state.status === "loading") return <Spinner />;
  if (state.status === "error") {
    return (
      <div className="flex flex-col gap-2">
        <Banner tone="error">{state.error}</Banner>
        {retry && (
          <div>
            <Button onClick={retry}>Retry</Button>
          </div>
        )}
      </div>
    );
  }
  return <>{children(state.data)}</>;
}

export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="rounded-md border border-dashed border-slate-300 p-8 text-center dark:border-slate-700">
      <p className="text-sm font-medium text-slate-700 dark:text-slate-200">{title}</p>
      {children && <div className="mt-1 text-sm text-slate-500 dark:text-slate-400">{children}</div>}
    </div>
  );
}

export const thClass = "px-3 py-2 text-left text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400";
export const tdClass = "px-3 py-2 text-sm text-slate-800 dark:text-slate-200";
