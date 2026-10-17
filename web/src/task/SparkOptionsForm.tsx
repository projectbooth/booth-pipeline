import { useState } from "react";
import type { SparkOptions, SparkStorageLocation } from "../types";
import { Button, Field, inputClass, monoClass } from "../ui/primitives";

// ADR 0119: what a Spark task asks booth-spark for. Its code is the task's own saved PySpark, sent
// inline; Spark checks its conf allowlist and its limits when the run starts, and a refusal fails the
// attempt with Spark's own message.

/** booth-spark's GET /v1/limits for this caller. Provisional: the route isn't in booth-spark 0.3.0 (it is
 *  coming in 0.3.1), so these names follow its design note until the real body is published; with no
 *  answer the panel simply shows no hint. */
export interface SparkLimits {
  maxExecutors?: number;
  maxMemory?: string;
  maxDuration?: string;
}

export function SparkOptionsForm({
  value,
  limits,
  error,
  onChange,
}: {
  value: SparkOptions;
  limits?: SparkLimits | null;
  error?: string;
  onChange: (v: SparkOptions) => void;
}) {
  const res = value.resources ?? {};
  const driver = res.driver ?? {};
  const ex = res.executors ?? {};
  const data = value.dataAccess ?? {};
  const storage = data.storage ?? [];
  const [confText, setConfText] = useState(() => Object.entries(value.conf ?? {}).map(([k, v]) => `${k}=${v}`).join("\n"));
  const [argsText, setArgsText] = useState(() => (value.args ?? []).join("\n"));

  const setRes = (r: SparkOptions["resources"]) => onChange({ ...value, resources: r });
  const setData = (d: SparkOptions["dataAccess"]) => onChange({ ...value, dataAccess: d });
  const num = (s: string) => (s.trim() === "" ? undefined : Number(s));
  const text = (s: string) => (s.trim() === "" ? undefined : s.trim());
  const setStorage = (i: number, loc: SparkStorageLocation) => setData({ ...data, storage: storage.map((s, j) => (j === i ? loc : s)) });

  return (
    <section aria-label="Spark options" className="flex flex-col gap-3 rounded-md border border-slate-200 p-3 dark:border-slate-700">
      <h3 className="text-xs font-medium text-slate-600 dark:text-slate-300">Spark</h3>
      <p className="-mt-1 text-xs text-slate-500 dark:text-slate-400">
        This task's code runs on booth-spark as PySpark (<code>ctx.spark</code> is its SparkSession), acting as the run: its owner's access, capped by the
        platform access cap. Its driver log is shown to editors and owners only.
        {limits && (limits.maxExecutors !== undefined || limits.maxMemory || limits.maxDuration) && (
          <>
            {" "}
            booth-spark allows{limits.maxExecutors !== undefined ? ` up to ${limits.maxExecutors} executors` : ""}
            {limits.maxMemory ? `, ${limits.maxMemory} per JVM` : ""}
            {limits.maxDuration ? `, ${limits.maxDuration} per run` : ""}.
          </>
        )}
      </p>
      {error && (
        <p role="alert" className="text-xs text-red-700 dark:text-red-400">
          {error}
        </p>
      )}
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Field id="spark-driver-memory" label="Driver memory">
          {(p) => <input {...p} className={inputClass} placeholder="1g" value={driver.memory ?? ""} onChange={(e) => setRes({ ...res, driver: { ...driver, memory: text(e.target.value) } })} />}
        </Field>
        <Field id="spark-executors-min" label="Executors (min)">
          {(p) => <input {...p} type="number" min={0} className={inputClass} value={ex.min ?? ""} onChange={(e) => setRes({ ...res, executors: { ...ex, min: num(e.target.value) } })} />}
        </Field>
        <Field id="spark-executors-max" label="Executors (max)">
          {(p) => <input {...p} type="number" min={0} className={inputClass} value={ex.max ?? ""} onChange={(e) => setRes({ ...res, executors: { ...ex, max: num(e.target.value) } })} />}
        </Field>
        <Field id="spark-executor-memory" label="Executor memory">
          {(p) => <input {...p} className={inputClass} placeholder="2g" value={ex.memory ?? ""} onChange={(e) => setRes({ ...res, executors: { ...ex, memory: text(e.target.value) } })} />}
        </Field>
      </div>
      <Field id="spark-conf" label="Spark settings" help="One key=value per line, e.g. spark.sql.shuffle.partitions=8. booth-spark allows only a few keys.">
        {(p) => (
          <textarea
            {...p}
            className={`${inputClass} ${monoClass} h-16 resize-y`}
            spellCheck={false}
            value={confText}
            onChange={(e) => {
              setConfText(e.target.value);
              const conf: Record<string, string> = {};
              for (const line of e.target.value.split(/\r?\n/)) {
                const i = line.indexOf("=");
                if (i > 0) conf[line.slice(0, i).trim()] = line.slice(i + 1).trim();
              }
              onChange({ ...value, conf });
            }}
          />
        )}
      </Field>
      <Field id="spark-args" label="Arguments" help="One per line; a plain script reads them as sys.argv[1:].">
        {(p) => (
          <textarea
            {...p}
            className={`${inputClass} ${monoClass} h-16 resize-y`}
            spellCheck={false}
            value={argsText}
            onChange={(e) => {
              setArgsText(e.target.value);
              onChange({ ...value, args: e.target.value.split(/\r?\n/).filter((a) => a !== "") });
            }}
          />
        )}
      </Field>
      <fieldset className="flex flex-col gap-2">
        <legend className="text-xs font-medium text-slate-600 dark:text-slate-300">Data the run may use</legend>
        <label className="flex items-center gap-2 text-sm text-slate-700 dark:text-slate-300">
          <input type="checkbox" checked={!!data.database} onChange={(e) => setData({ ...data, database: e.target.checked })} />
          The workspace's database
        </label>
        <label className="flex items-center gap-2 text-sm text-slate-700 dark:text-slate-300">
          <input type="checkbox" checked={!!data.lakehouse} onChange={(e) => setData({ ...data, lakehouse: e.target.checked })} />
          The workspace's lakehouse (Iceberg catalog <code>lakehouse</code>)
        </label>
        {storage.map((s, i) => (
          <div key={i} role="group" aria-label={`Storage location ${i + 1}`} className="flex flex-wrap items-center gap-2">
            <input aria-label="Storage backend" className={`${inputClass} w-40`} placeholder="backend id" value={s.backendId} onChange={(e) => setStorage(i, { ...s, backendId: e.target.value })} />
            <input aria-label="Path" className={`${inputClass} w-56 ${monoClass}`} placeholder="path/prefix" value={s.path} onChange={(e) => setStorage(i, { ...s, path: e.target.value })} />
            <select aria-label="Access" className={`${inputClass} w-36`} value={s.access ?? "read"} onChange={(e) => setStorage(i, { ...s, access: e.target.value as "read" | "readwrite" })}>
              <option value="read">Read</option>
              <option value="readwrite">Read and write</option>
            </select>
            <Button size="sm" onClick={() => setData({ ...data, storage: storage.filter((_, j) => j !== i) })}>
              Remove
            </Button>
          </div>
        ))}
        {storage.length < 4 && (
          <div>
            <Button size="sm" onClick={() => setData({ ...data, storage: [...storage, { backendId: "", path: "", access: "read" }] })}>
              + Storage location
            </Button>
          </div>
        )}
      </fieldset>
    </section>
  );
}
