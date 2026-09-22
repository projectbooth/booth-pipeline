import { useCallback, useEffect, useRef, useState } from "react";
import { api, type ApiContext } from "../api/client";
import { errorMessage, useInterval } from "../hooks";
import type { LogLine } from "../types";
import { Banner } from "./ui";

// Run logs for one Run — the whole run, or a single Task's lines. Polls the server with the
// `seq` cursor ("everything after N") so each poll fetches only what is new, and stops when the
// server says `done` (the run is finished AND nothing more can arrive — see api.py's run_logs for
// why that flag is only ever read after the run status, never before it).

const POLL_MS = 1500;
const MAX_KEPT = 20_000; // the UI's own bound, on top of the server's per-run line cap

const STREAM_CLASS: Record<LogLine["stream"], string> = {
  stdout: "text-slate-800 dark:text-slate-200",
  stderr: "text-red-700 dark:text-red-400",
  system: "italic text-slate-500 dark:text-slate-400",
};

function clock(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleTimeString([], { hour12: false }) + "." + String(d.getMilliseconds()).padStart(3, "0");
}

export function LogViewer({ api: ctx, runId, taskKey, runActive }: { api: ApiContext; runId: string; taskKey: string | null; runActive: boolean }) {
  const [lines, setLines] = useState<LogLine[]>([]);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const cursor = useRef(0);
  const generation = useRef(0);
  const inFlight = useRef(false);
  const box = useRef<HTMLDivElement>(null);
  const stick = useRef(true);

  // A different run or task: start over from the top.
  useEffect(() => {
    generation.current++;
    cursor.current = 0;
    inFlight.current = false;
    stick.current = true;
    setLines([]);
    setDone(false);
    setError(null);
  }, [runId, taskKey]);

  const poll = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    const mine = generation.current;
    try {
      // Drain: keep going while pages come back full, so a long log loads at once.
      for (;;) {
        const page = await api.runLogs(ctx, runId, { task: taskKey ?? undefined, after: cursor.current });
        if (mine !== generation.current) return; // superseded by a newer run/task selection
        if (page.items.length > 0) {
          cursor.current = page.items[page.items.length - 1].seq;
          setLines((prev) => {
            const next = prev.concat(page.items);
            return next.length > MAX_KEPT ? next.slice(next.length - MAX_KEPT) : next;
          });
        }
        setError(null);
        if (page.done) setDone(true);
        if (page.items.length < 1000) break;
      }
    } catch (err) {
      if (mine === generation.current) setError(errorMessage(err));
    } finally {
      if (mine === generation.current) inFlight.current = false;
    }
  }, [ctx, runId, taskKey]);

  // First load, and whenever the selection changes.
  useEffect(() => {
    void poll();
  }, [poll, runId, taskKey]);

  // Poll until the server says `done`. There is no need to also key on `runActive`: when the run
  // ends, the next poll fetches the final lines and returns done=true, which stops this.
  useInterval(() => void poll(), POLL_MS, !done);

  useEffect(() => {
    const el = box.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [lines]);

  return (
    <div className="flex flex-col gap-2">
      {error && <Banner tone="error">Could not load logs: {error}</Banner>}
      <div
        ref={box}
        role="log"
        aria-label={taskKey ? `Logs for task ${taskKey}` : "Logs for the whole run"}
        tabIndex={0}
        onScroll={(e) => {
          const el = e.currentTarget;
          stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
        }}
        className="h-72 overflow-auto rounded-md border border-slate-200 bg-slate-50 p-2 font-mono text-xs leading-relaxed dark:border-slate-700 dark:bg-slate-950"
      >
        {lines.length === 0 ? (
          <p className="text-slate-500 dark:text-slate-400">{done ? "No log output." : runActive ? "Waiting for output…" : "No log output yet."}</p>
        ) : (
          lines.map((ln) => (
            <div key={ln.seq} className={`flex gap-2 whitespace-pre-wrap break-words ${STREAM_CLASS[ln.stream]}`}>
              <span className="shrink-0 text-slate-400 dark:text-slate-500">{clock(ln.ts)}</span>
              {taskKey === null && <span className="shrink-0 text-indigo-600 dark:text-indigo-400">{ln.taskKey ?? "run"}</span>}
              {ln.attempt > 1 && <span className="shrink-0 text-amber-600 dark:text-amber-400">#{ln.attempt}</span>}
              <span className="min-w-0">{ln.message}</span>
            </div>
          ))
        )}
      </div>
    </div>
  );
}
