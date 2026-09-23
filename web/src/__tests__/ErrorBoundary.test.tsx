import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ErrorBoundary } from "../components/ErrorBoundary";

// The gap a 2026-09-23 pilot test found: nothing anywhere in this package caught a render-time
// exception, so one crashed component unmounted the entire app to a blank screen with no visible
// error and no diagnostic trail.

function Boom(): never {
  throw new Error("kaboom");
}

function Fine() {
  return <p>all good</p>;
}

describe("ErrorBoundary", () => {
  it("shows an error banner instead of unmounting the whole tree, and logs the real error", () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    );
    expect(screen.getByText("Something went wrong showing this page.")).toBeInTheDocument();
    expect(screen.getByText("kaboom")).toBeInTheDocument();
    expect(spy).toHaveBeenCalled();
    spy.mockRestore();
  });

  it("renders children normally when nothing throws", () => {
    render(
      <ErrorBoundary>
        <Fine />
      </ErrorBoundary>,
    );
    expect(screen.getByText("all good")).toBeInTheDocument();
    expect(screen.queryByText("Something went wrong showing this page.")).not.toBeInTheDocument();
  });

  it("'Try again' clears the error and re-renders the same children", async () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    const user = userEvent.setup();
    // An external flag, not a render-count: React 18 invokes a function component more than once
    // per commit in development (its own impure-render detection), so counting calls would flip
    // before "Try again" is ever clicked. "Try again" re-renders the same children; it is the
    // test that decides the underlying cause is now gone, the same way it would be after fixing
    // the bad response shape that threw in the first place.
    let shouldThrow = true;
    function Toggle() {
      if (shouldThrow) throw new Error("first render fails");
      return <p>recovered</p>;
    }
    render(
      <ErrorBoundary>
        <Toggle />
      </ErrorBoundary>,
    );
    expect(screen.getByText("Something went wrong showing this page.")).toBeInTheDocument();
    shouldThrow = false;
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(screen.getByText("recovered")).toBeInTheDocument();
    spy.mockRestore();
  });
});
