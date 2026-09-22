import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";
import { ScheduleEditor, type TriggerShape } from "../components/ScheduleEditor";

// A thin controlled wrapper so each test can read back what the editor last emitted, the same way
// JobForm consumes it.
function Harness({ initial }: { initial: TriggerShape }) {
  const [value, setValue] = useState<TriggerShape>(initial);
  return (
    <div>
      <ScheduleEditor value={value} onChange={setValue} fallbackTimezone="UTC" fieldError={() => undefined} />
      <pre data-testid="out">{JSON.stringify(value)}</pre>
    </div>
  );
}

function out() {
  return JSON.parse(screen.getByTestId("out").textContent ?? "null") as TriggerShape;
}

describe("ScheduleEditor", () => {
  it("defaults to the friendly 'Daily' view for a plain daily cron and edits it by time, not text", () => {
    render(<Harness initial={{ type: "cron", cron: "0 9 * * *", timezone: "America/Toronto" }} />);
    expect(screen.getByLabelText("Frequency")).toHaveValue("daily");
    const time = screen.getByLabelText("Time") as HTMLInputElement;
    expect(time.value).toBe("09:00");
    fireEvent.change(time, { target: { value: "14:30" } });
    expect(out()).toEqual({ type: "cron", cron: "30 14 * * *", timezone: "America/Toronto" });
  });

  it("recognizes a single-weekday cron as 'Weekly' and edits day + time", async () => {
    const user = userEvent.setup();
    render(<Harness initial={{ type: "cron", cron: "0 9 * * 1", timezone: "UTC" }} />);
    expect(screen.getByLabelText("Frequency")).toHaveValue("weekly");
    await user.selectOptions(screen.getByLabelText("Day"), "Wednesday");
    expect(out()).toEqual({ type: "cron", cron: "0 9 * * 3", timezone: "UTC" });
  });

  it("recognizes an hour-repeat cron as 'Every N hours'", () => {
    render(<Harness initial={{ type: "cron", cron: "0 */4 * * *", timezone: "UTC" }} />);
    expect(screen.getByLabelText("Frequency")).toHaveValue("hourly");
    expect(screen.getByLabelText("Every how many hours")).toHaveValue(4);
  });

  it("recognizes a minute-repeat cron as 'Every N minutes'", () => {
    render(<Harness initial={{ type: "cron", cron: "*/15 * * * *", timezone: "UTC" }} />);
    expect(screen.getByLabelText("Frequency")).toHaveValue("minutes");
    expect(screen.getByLabelText("Every how many minutes")).toHaveValue(15);
  });

  it("falls back to 'Custom' for a cron shape none of the friendly pickers can express, and never loses it", () => {
    render(<Harness initial={{ type: "cron", cron: "0 9 1 * *", timezone: "UTC" }} />); // day-of-month, not expressible
    expect(screen.getByLabelText("Frequency")).toHaveValue("custom");
    expect(screen.getByLabelText(/Cron expression/)).toHaveValue("0 9 1 * *");
  });

  it("an interval trigger opens directly on 'Every N seconds' with no timezone field", () => {
    render(<Harness initial={{ type: "interval", seconds: 30 }} />);
    expect(screen.getByLabelText("Frequency")).toHaveValue("seconds");
    expect(screen.getByLabelText("Every how many seconds")).toHaveValue(30);
    expect(screen.queryByLabelText("Timezone")).not.toBeInTheDocument();
  });

  it("switching frequency to 'Every N seconds' emits an IntervalTrigger shape", async () => {
    const user = userEvent.setup();
    render(<Harness initial={{ type: "cron", cron: "0 9 * * *", timezone: "UTC" }} />);
    await user.selectOptions(screen.getByLabelText("Frequency"), "Every N seconds");
    expect(out()).toEqual({ type: "interval", seconds: 5 });
    fireEvent.change(screen.getByLabelText("Every how many seconds"), { target: { value: "45" } });
    expect(out()).toEqual({ type: "interval", seconds: 45 });
  });

  it("switching frequency back to a calendar option from seconds produces a cron shape again", async () => {
    const user = userEvent.setup();
    render(<Harness initial={{ type: "interval", seconds: 30 }} />);
    await user.selectOptions(screen.getByLabelText("Frequency"), "Daily, at a time");
    expect(out()).toEqual({ type: "cron", cron: "0 9 * * *", timezone: "UTC" });
  });
});
