import type { ViewCtx } from "../../context";
import { describeInterval, describeSchedule, formatRelative, formatStamp, formatTime } from "../../format";
import { Banner, Card, KVRows, type KV } from "../../ui/primitives";
import type { PipelineData } from "./data";

// S2d — the Schedule & Triggers tab (docs/decisions/0016 §4, Figma screen 5). The read-only summary
// is the default view; its in-place editor arrives in phase 3.

export function ScheduleTab({ d }: { v: ViewCtx; d: PipelineData }) {
  const p = d.pipeline;
  const s = p.schedule;
  const pinned = p.pinnedVersion ? d.versions.find((ver) => ver.version === p.pinnedVersion) : undefined;

  const rows: KV[] = [
    { label: "Trigger type", value: !s ? "None — manual only" : s.type === "cron" ? "Cron" : "Interval", mono: true },
    ...(s?.type === "cron"
      ? [
          { label: "Expression", value: <>{s.cron} <span className="font-sans text-slate-500 dark:text-slate-400">— {describeSchedule(s)}</span></>, mono: true },
          { label: "Timezone", value: s.timezone, mono: true },
        ]
      : s?.type === "interval"
        ? [{ label: "Every", value: describeInterval(s.seconds).replace(/^every /, ""), mono: true }]
        : []),
    ...(s ? [{ label: "Enabled", value: s.enabled ? "Yes" : "No — paused" }] : []),
    { label: "Next run", value: p.nextRunAt ? <span title={formatRelative(p.nextRunAt)}>{formatTime(p.nextRunAt)}</span> : "—", mono: true },
    {
      label: "Runs version",
      value: p.pinnedVersion ? `Pinned to v${p.pinnedVersion}${pinned ? ` — ${formatStamp(pinned.createdAt)}` : ""}` : "Always the latest saved version",
    },
    { label: "Concurrent runs", value: p.allowConcurrentRuns ? "Allowed" : "Not allowed — a new run is refused while one is active" },
    { label: "Platform access cap", value: p.roleCeiling === "editor" ? "Editor — can read and write storage and the catalog" : "Viewer — read only" },
  ];

  return (
    <div className="flex max-w-3xl flex-col gap-4">
      <Card title="Trigger">
        <KVRows rows={rows} />
      </Card>
      {s && s.enabled ? (
        <Banner tone="info">
          <strong>Scheduled.</strong> Runs {describeSchedule(s)}.{p.nextRunAt ? ` Next: ${formatTime(p.nextRunAt)}.` : ""}
        </Banner>
      ) : (
        <Banner tone="info">
          <strong>{s ? "Paused." : "Manual only."}</strong> This pipeline runs when someone presses Run now, above.
        </Banner>
      )}
      {!p.hasOwner && s && (
        <Banner tone="warn">This pipeline was scheduled before workload identity and has no owner, so its unattended runs get no platform access until its schedule is saved again.</Banner>
      )}
    </div>
  );
}
