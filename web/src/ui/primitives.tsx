import { useEffect, useRef, useState, type AnchorHTMLAttributes, type ButtonHTMLAttributes, type InputHTMLAttributes, type MouseEvent, type ReactNode } from "react";
import type { LoadState } from "../hooks";
import type { RunStatus, TaskStatus } from "../types";

// Presentational primitives for the rebuilt UI (docs/decisions/0016). Layout and hierarchy follow
// the Figma reference (stat cards, key/value cards, underline tabs, status dots); colour stays on
// the Tailwind slate/indigo palette booth-catalog, booth-storage and booth-module-store share
// (decision 0004), so the native modules look alike in the shell. Every colour has a `dark:`
// counterpart, driven by the `data-theme` attribute PipelineApp sets from its `theme` prop.

// ---- buttons, inputs, fields --------------------------------------------------------------------

type Variant = "primary" | "secondary" | "danger" | "ghost";

const VARIANTS: Record<Variant, string> = {
  primary: "bg-indigo-600 text-white hover:bg-indigo-500 disabled:opacity-50 disabled:hover:bg-indigo-600",
  secondary:
    "border border-slate-300 bg-white text-slate-700 hover:bg-slate-50 disabled:opacity-50 dark:border-slate-600 dark:bg-slate-900 dark:text-slate-200 dark:hover:bg-slate-800",
  danger:
    "border border-red-300 bg-white text-red-700 hover:bg-red-50 disabled:opacity-50 dark:border-red-800 dark:bg-slate-900 dark:text-red-400 dark:hover:bg-red-950",
  ghost: "text-slate-600 hover:bg-slate-100 disabled:opacity-50 dark:text-slate-300 dark:hover:bg-slate-800",
};

export function Button({
  variant = "secondary",
  size = "md",
  className = "",
  type = "button",
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant; size?: "sm" | "md" }) {
  const sizing = size === "sm" ? "px-2 py-1 text-xs" : "px-3 py-1.5 text-sm";
  return (
    <button
      type={type}
      className={`inline-flex items-center justify-center gap-1.5 whitespace-nowrap rounded-md font-medium focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500 disabled:cursor-not-allowed ${sizing} ${VARIANTS[variant]} ${className}`}
      {...rest}
    />
  );
}

export const inputClass =
  "w-full rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm text-slate-900 placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 disabled:opacity-60 dark:border-slate-600 dark:bg-slate-900 dark:text-slate-100 dark:placeholder:text-slate-500";

/** `inputClass` sized to its content instead of its container — for selects that sit in a row. */
export const inputInlineClass = inputClass.replace("w-full ", "w-auto max-w-full ");

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
  help?: ReactNode;
  error?: string;
  required?: boolean;
  children: (props: { id: string; "aria-describedby"?: string; "aria-invalid"?: boolean }) => ReactNode;
}) {
  const describedBy = [help ? `${id}-help` : "", error ? `${id}-error` : ""].filter(Boolean).join(" ") || undefined;
  return (
    <div className="flex min-w-0 flex-col gap-1">
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

// ---- feedback -----------------------------------------------------------------------------------

export function Banner({ tone, children, action }: { tone: "error" | "info" | "success" | "warn"; children: ReactNode; action?: ReactNode }) {
  const tones = {
    error: "border-red-200 bg-red-50 text-red-800 dark:border-red-900 dark:bg-red-950/60 dark:text-red-200",
    info: "border-sky-200 bg-sky-50 text-sky-900 dark:border-sky-900 dark:bg-sky-950/50 dark:text-sky-200",
    success: "border-emerald-200 bg-emerald-50 text-emerald-800 dark:border-emerald-900 dark:bg-emerald-950/50 dark:text-emerald-200",
    warn: "border-amber-200 bg-amber-50 text-amber-900 dark:border-amber-900 dark:bg-amber-950/50 dark:text-amber-200",
  };
  return (
    <div role={tone === "error" ? "alert" : "status"} className={`flex items-start justify-between gap-3 rounded-md border px-3 py-2 text-sm ${tones[tone]}`}>
      <div className="min-w-0">{children}</div>
      {action && <div className="shrink-0">{action}</div>}
    </div>
  );
}

/** An inline "are you sure?" row: rendered in the page flow where the action was, never a modal
 *  and never `window.confirm` (docs/decisions/0016 §4 — nothing in this module draws over anything). */
export function InlineConfirm({
  message,
  confirmLabel,
  busyLabel,
  onConfirm,
  onCancel,
  busy,
  tone = "danger",
}: {
  message: ReactNode;
  confirmLabel: string;
  busyLabel?: string;
  onConfirm: () => void;
  onCancel: () => void;
  busy?: boolean;
  tone?: "danger" | "primary";
}) {
  return (
    <div role="group" aria-label="Confirm" className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-slate-300 bg-slate-50 px-3 py-2 text-sm dark:border-slate-600 dark:bg-slate-900">
      <span className="text-slate-700 dark:text-slate-200">{message}</span>
      <span className="flex gap-2">
        <Button size="sm" onClick={onCancel} disabled={busy}>
          Cancel
        </Button>
        <Button size="sm" variant={tone} onClick={onConfirm} disabled={busy}>
          {busy ? (busyLabel ?? `${confirmLabel}…`) : confirmLabel}
        </Button>
      </span>
    </div>
  );
}

// ---- badges -------------------------------------------------------------------------------------

export type Tone = "slate" | "indigo" | "amber" | "emerald" | "sky" | "red";

const BADGE_TONES: Record<Tone, string> = {
  slate: "bg-slate-100 text-slate-700 ring-slate-200 dark:bg-slate-800 dark:text-slate-300 dark:ring-slate-700",
  indigo: "bg-indigo-50 text-indigo-700 ring-indigo-200 dark:bg-indigo-950 dark:text-indigo-300 dark:ring-indigo-900",
  amber: "bg-amber-50 text-amber-800 ring-amber-200 dark:bg-amber-950 dark:text-amber-300 dark:ring-amber-900",
  emerald: "bg-emerald-50 text-emerald-700 ring-emerald-200 dark:bg-emerald-950 dark:text-emerald-300 dark:ring-emerald-900",
  sky: "bg-sky-50 text-sky-700 ring-sky-200 dark:bg-sky-950 dark:text-sky-300 dark:ring-sky-900",
  red: "bg-red-50 text-red-700 ring-red-200 dark:bg-red-950 dark:text-red-300 dark:ring-red-900",
};

export function Badge({ children, tone = "slate", mono, title }: { children: ReactNode; tone?: Tone; mono?: boolean; title?: string }) {
  return (
    <span
      title={title}
      className={`inline-flex items-center gap-1 whitespace-nowrap rounded px-1.5 py-0.5 text-xs font-medium ring-1 ring-inset ${mono ? "font-mono uppercase tracking-wide" : ""} ${BADGE_TONES[tone]}`}
    >
      {children}
    </span>
  );
}

export type AnyStatus = RunStatus | TaskStatus | "never";

/** docs/decisions/0016 §1: green = succeeded, red = failed, blue = running/retrying, amber =
 *  skipped, grey = queued/pending, slate = canceled. Shared by badges, DAG borders and the runs strip. */
export const STATUS_STYLE: Record<AnyStatus, { tone: Tone; label: string; dot: string; border: string; bar: string }> = {
  succeeded: { tone: "emerald", label: "Succeeded", dot: "bg-emerald-500", border: "border-emerald-500", bar: "bg-emerald-500" },
  failed: { tone: "red", label: "Failed", dot: "bg-red-500", border: "border-red-500", bar: "bg-red-500" },
  running: { tone: "sky", label: "Running", dot: "bg-sky-500 animate-pulse", border: "border-sky-500", bar: "bg-sky-500" },
  retrying: { tone: "sky", label: "Retrying", dot: "bg-sky-500 animate-pulse", border: "border-sky-500", bar: "bg-sky-500" },
  queued: { tone: "slate", label: "Queued", dot: "bg-slate-400", border: "border-slate-400 border-dashed", bar: "bg-slate-400" },
  pending: { tone: "slate", label: "Pending", dot: "bg-slate-400", border: "border-slate-400 border-dashed", bar: "bg-slate-400" },
  skipped: { tone: "amber", label: "Skipped", dot: "bg-amber-500", border: "border-amber-500", bar: "bg-amber-500" },
  canceled: { tone: "slate", label: "Canceled", dot: "bg-slate-500", border: "border-slate-500", bar: "bg-slate-500" },
  never: { tone: "slate", label: "Never run", dot: "border border-slate-400 bg-transparent", border: "border-slate-300 dark:border-slate-600", bar: "bg-slate-300" },
};

/** Status is never conveyed by colour alone: the word is always shown. */
export function StatusBadge({ status }: { status: AnyStatus }) {
  const s = STATUS_STYLE[status];
  return (
    <Badge tone={s.tone}>
      <span className={`h-1.5 w-1.5 rounded-full ${s.dot}`} aria-hidden="true" />
      {s.label}
    </Badge>
  );
}

// ---- layout -------------------------------------------------------------------------------------

/** Page title block for list pages and detail pages alike. `crumbs` renders the "← Pipelines / X"
 *  trail above the title on detail pages. */
export function PageHeader({
  crumbs,
  title,
  titleExtra,
  subtitle,
  actions,
  below,
}: {
  crumbs?: ReactNode;
  title: ReactNode;
  titleExtra?: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  below?: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      {crumbs && <nav aria-label="Breadcrumb" className="text-sm text-slate-500 dark:text-slate-400">{crumbs}</nav>}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-1">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <h2 className="break-words text-xl font-semibold text-slate-900 dark:text-slate-50">{title}</h2>
            {titleExtra}
          </div>
          {subtitle && <p className="text-sm text-slate-500 dark:text-slate-400">{subtitle}</p>}
        </div>
        {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
      </div>
      {below}
    </div>
  );
}

/** One cell of the Figma's metadata strip: uppercase label, mono value. */
export interface Stat {
  label: string;
  value: ReactNode;
  title?: string;
  mono?: boolean;
}

export function StatStrip({ stats, label }: { stats: Stat[]; label: string }) {
  return (
    <dl
      aria-label={label}
      className="grid grid-cols-2 overflow-hidden rounded-lg border border-slate-200 bg-white sm:grid-cols-3 lg:grid-cols-6 dark:border-slate-700 dark:bg-slate-900"
    >
      {stats.map((s) => (
        <div key={s.label} className="min-w-0 border-b border-r border-slate-200 px-4 py-3 dark:border-slate-700">
          <dt className="text-[11px] font-semibold uppercase tracking-wider text-slate-500 dark:text-slate-400">{s.label}</dt>
          <dd title={s.title} className={`mt-1 truncate text-sm text-slate-900 dark:text-slate-100 ${s.mono === false ? "" : "font-mono"}`}>
            {s.value}
          </dd>
        </div>
      ))}
    </dl>
  );
}

/** A single big-number card (Runs tab). */
export function StatCard({ label, value, tone = "slate", children }: { label: string; value?: ReactNode; tone?: Tone; children?: ReactNode }) {
  const text: Record<Tone, string> = {
    slate: "text-slate-700 dark:text-slate-200",
    indigo: "text-indigo-600 dark:text-indigo-400",
    amber: "text-amber-600 dark:text-amber-400",
    emerald: "text-emerald-600 dark:text-emerald-400",
    sky: "text-sky-600 dark:text-sky-400",
    red: "text-red-600 dark:text-red-400",
  };
  return (
    <div className="min-w-0 rounded-lg border border-slate-200 bg-white px-4 py-3 dark:border-slate-700 dark:bg-slate-900">
      <p className="text-[11px] font-semibold uppercase tracking-wider text-slate-500 dark:text-slate-400">{label}</p>
      {value !== undefined && <p className={`mt-1 font-mono text-2xl font-semibold ${text[tone]}`}>{value}</p>}
      {children}
    </div>
  );
}

/** A bordered card with a header row — the Figma's "General" / "Trigger" / "Task Settings" boxes. */
export function Card({ title, actions, children, className = "", label }: { title?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string; label?: string }) {
  return (
    <section aria-label={label ?? (typeof title === "string" ? title : undefined)} className={`overflow-hidden rounded-lg border border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-900 ${className}`}>
      {(title || actions) && (
        <div className="flex items-center justify-between gap-2 border-b border-slate-200 px-4 py-2.5 dark:border-slate-700">
          <h3 className="text-sm font-semibold text-slate-800 dark:text-slate-100">{title}</h3>
          {actions && <div className="flex items-center gap-2">{actions}</div>}
        </div>
      )}
      {children}
    </section>
  );
}

export interface KV {
  label: string;
  value: ReactNode;
  mono?: boolean;
}

/** Flat, read-only key/value rows inside a Card. */
export function KVRows({ rows }: { rows: KV[] }) {
  return (
    <dl className="divide-y divide-slate-100 dark:divide-slate-800">
      {rows.map((r) => (
        <div key={r.label} className="grid grid-cols-1 gap-1 px-4 py-2.5 sm:grid-cols-[minmax(10rem,14rem)_1fr] sm:gap-4">
          <dt className="text-sm text-slate-500 dark:text-slate-400">{r.label}</dt>
          <dd className={`min-w-0 break-words text-sm text-slate-900 dark:text-slate-100 ${r.mono ? "font-mono" : ""}`}>{r.value}</dd>
        </div>
      ))}
    </dl>
  );
}

/** Underline tabs (the Figma's DAG / Runs / … bar). Real links, so tabs are deep-linkable and
 *  open in a new browser tab like any other link. */
export function Tabs<K extends string>({
  label,
  tabs,
  active,
  hrefFor,
  onNavigate,
}: {
  label: string;
  tabs: { key: K; label: string }[];
  active: K;
  hrefFor: (key: K) => string;
  onNavigate: (path: string) => void;
}) {
  return (
    <nav aria-label={label} className="-mb-px flex gap-1 overflow-x-auto border-b border-slate-200 dark:border-slate-700">
      {tabs.map((t) => (
        <Link
          key={t.key}
          href={hrefFor(t.key)}
          onNavigate={onNavigate}
          aria-current={active === t.key ? "page" : undefined}
          className={`whitespace-nowrap border-b-2 px-3 py-2 text-sm ${
            active === t.key
              ? "border-indigo-600 font-medium text-indigo-600 dark:border-indigo-400 dark:text-indigo-400"
              : "border-transparent text-slate-500 hover:border-slate-300 hover:text-slate-800 dark:text-slate-400 dark:hover:border-slate-600 dark:hover:text-slate-200"
          }`}
        >
          {t.label}
        </Link>
      ))}
    </nav>
  );
}

/** A small overflow ("⋯") menu. Closes on outside click, Escape, or picking an item. */
export function OverflowMenu({ label, items }: { label: string; items: { label: string; onSelect: () => void; danger?: boolean; disabled?: boolean }[] }) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: globalThis.MouseEvent) => root.current && !root.current.contains(e.target as Node) && setOpen(false);
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);
  if (items.length === 0) return null;
  return (
    <div ref={root} className="relative">
      <Button aria-label={label} aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        ⋯
      </Button>
      {open && (
        <div role="menu" className="absolute right-0 z-20 mt-1 min-w-44 overflow-hidden rounded-md border border-slate-200 bg-white py-1 shadow-lg dark:border-slate-700 dark:bg-slate-900">
          {items.map((it) => (
            <button
              key={it.label}
              type="button"
              role="menuitem"
              disabled={it.disabled}
              onClick={() => {
                setOpen(false);
                it.onSelect();
              }}
              className={`block w-full px-3 py-1.5 text-left text-sm disabled:opacity-50 ${
                it.danger ? "text-red-700 hover:bg-red-50 dark:text-red-400 dark:hover:bg-red-950" : "text-slate-700 hover:bg-slate-100 dark:text-slate-200 dark:hover:bg-slate-800"
              }`}
            >
              {it.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

// ---- tables -------------------------------------------------------------------------------------

export const tableClass = "w-full text-left";
export const theadClass = "border-b border-slate-200 dark:border-slate-700";
export const thClass = "whitespace-nowrap px-3 py-2 text-[11px] font-semibold uppercase tracking-wider text-slate-500 dark:text-slate-400";
export const tdClass = "px-3 py-2.5 align-middle text-sm text-slate-800 dark:text-slate-200";
export const tbodyClass = "divide-y divide-slate-100 dark:divide-slate-800";
export const rowHoverClass = "hover:bg-slate-50 dark:hover:bg-slate-800/50";

// ---- navigation ---------------------------------------------------------------------------------

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

// ---- loading / empty ----------------------------------------------------------------------------

/** A placeholder block with the rough shape of what is loading, so the page doesn't jump. */
export function Skeleton({ className = "h-4 w-full" }: { className?: string }) {
  return <div aria-hidden="true" className={`animate-pulse rounded bg-slate-200 dark:bg-slate-800 ${className}`} />;
}

export function PageSkeleton() {
  return (
    <div role="status" aria-label="Loading" className="flex flex-col gap-4">
      <Skeleton className="h-6 w-64" />
      <Skeleton className="h-4 w-96 max-w-full" />
      <Skeleton className="h-20 w-full" />
      <Skeleton className="h-64 w-full" />
    </div>
  );
}

/** Renders a `useLoad` state: skeleton, error banner (with retry), or the loaded content. */
export function Loaded<T>({ state, retry, children, skeleton }: { state: LoadState<T>; retry?: () => void; children: (data: T) => ReactNode; skeleton?: ReactNode }) {
  if (state.status === "loading") return <>{skeleton ?? <PageSkeleton />}</>;
  if (state.status === "error") {
    return (
      <Banner tone="error" action={retry && <Button size="sm" onClick={retry}>Retry</Button>}>
        {state.error}
      </Banner>
    );
  }
  return <>{children(state.data)}</>;
}

export function EmptyState({ title, children, action }: { title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="flex flex-col items-center gap-2 rounded-lg border border-dashed border-slate-300 px-6 py-10 text-center dark:border-slate-700">
      <p className="text-sm font-medium text-slate-800 dark:text-slate-100">{title}</p>
      {children && <div className="max-w-md text-sm text-slate-500 dark:text-slate-400">{children}</div>}
      {action && <div className="mt-1">{action}</div>}
    </div>
  );
}

/** A play triangle as inline SVG — the "▶" character renders as a coloured emoji tile on Windows. */
export function PlayIcon({ className = "h-3 w-3" }: { className?: string }) {
  return (
    <svg viewBox="0 0 12 12" aria-hidden="true" className={`shrink-0 fill-current ${className}`}>
      <path d="M3 1.8v8.4a.6.6 0 0 0 .9.52l7-4.2a.6.6 0 0 0 0-1.04l-7-4.2A.6.6 0 0 0 3 1.8Z" />
    </svg>
  );
}

/** A number field that lets you type. A plain controlled `<input type="number">` that clamps on
 *  every keystroke snaps an emptied field straight back to its minimum, so selecting "30" and
 *  typing "120" yields "1120". This keeps the raw text while you type, commits only whole numbers
 *  inside [min, max], and tidies the text back to the committed value on blur. */
export function NumberInput({
  value,
  onChange,
  min,
  max,
  className = inputClass,
  ...rest
}: Omit<InputHTMLAttributes<HTMLInputElement>, "value" | "onChange" | "min" | "max" | "type"> & {
  value: number;
  onChange: (n: number) => void;
  min: number;
  max?: number;
}) {
  const [text, setText] = useState(String(value));
  const focused = useRef(false);
  // Follow outside changes (a reset, a reload) — but never fight the user mid-edit.
  useEffect(() => {
    if (!focused.current) setText(String(value));
  }, [value]);
  return (
    <input
      {...rest}
      type="number"
      inputMode="numeric"
      min={min}
      max={max}
      className={className}
      value={text}
      onFocus={() => {
        focused.current = true;
      }}
      onChange={(e) => {
        setText(e.target.value);
        const n = Number(e.target.value);
        if (e.target.value.trim() !== "" && Number.isInteger(n) && n >= min && (max === undefined || n <= max)) onChange(n);
      }}
      onBlur={() => {
        focused.current = false;
        setText(String(value));
      }}
    />
  );
}
