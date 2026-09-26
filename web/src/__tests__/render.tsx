import { render } from "@testing-library/react";
import { PipelineApp } from "../PipelineApp";
import type { WorkspaceRole } from "../types";
import { getToken, type FakeBackend } from "./testUtils";

/** Mounts PipelineApp exactly as booth-design does (the contract four props, nothing else), at
 *  `path`, against an installed FakeBackend. */
export function renderApp(backend: FakeBackend, { path = "/pipeline/pipelines", role = "owner", theme = "light" }: { path?: string; role?: WorkspaceRole; theme?: "light" | "dark" } = {}) {
  backend.install();
  window.history.replaceState({}, "", path);
  return render(<PipelineApp workspace="acme" role={role} theme={theme} getAccessToken={getToken} />);
}
