import { screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as publicApi from "../index";
import type { PipelineAppProps } from "../index";
import { renderApp } from "./render";
import { FakeBackend } from "./testUtils";

// ADR 0074's hard constraint: the ADR 0030/0031/0033 mount contract is unchanged by the rebuild,
// and the module adds no outer padding of its own (ADR 0072). booth-design must need nothing but
// a version-pin bump.

afterEach(() => vi.unstubAllGlobals());

describe("mount contract", () => {
  it("exports PipelineApp and nothing else at runtime", () => {
    expect(Object.keys(publicApi).sort()).toEqual(["PipelineApp"]);
  });

  it("accepts exactly the contract props, with basePath/onNavigate still optional", () => {
    // Compile-time: this object must type-check with only the four contract props.
    const props: PipelineAppProps = { workspace: "w", role: "viewer", theme: "dark", getAccessToken: () => null };
    expect(Object.keys(props)).toEqual(["workspace", "role", "theme", "getAccessToken"]);
  });

  it("sets data-theme on its root and adds no outer padding or margin", async () => {
    const { container } = renderApp(new FakeBackend(), { theme: "dark" });
    await screen.findByText("No pipelines yet");
    const root = container.firstElementChild as HTMLElement;
    expect(root).toHaveAttribute("data-theme", "dark");
    const spacing = root.className.split(/\s+/).filter((c) => /^-?(p|m)[xytrbl]?-/.test(c));
    expect(spacing).toEqual([]);
  });

  it("sends X-Workspace and a fresh bearer token on every request, through the gateway path", async () => {
    const b = new FakeBackend();
    renderApp(b);
    await screen.findByText("No pipelines yet");
    await waitFor(() => expect(b.calls.length).toBeGreaterThan(0));
    for (const c of b.calls) {
      expect(c.headers.get("X-Workspace")).toBe("acme");
      expect(c.headers.get("Authorization")).toBe("Bearer tok-test");
    }
    expect((fetch as unknown as { mock: { calls: [string][] } }).mock.calls.every(([url]) => url.startsWith("/modules/pipeline/api/"))).toBe(true);
  });
});
