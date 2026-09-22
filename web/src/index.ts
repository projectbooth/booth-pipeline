// Public entry point for @projectbooth/pipeline-ui (ADR 0030). Anything booth-design (or any future
// consumer) needs is re-exported here — internal components/helpers under src/views,
// src/components, src/api, etc. are not part of the public API and can change freely.
//
// Consumers must also import this package's stylesheet once
// (`@projectbooth/pipeline-ui/dist/style.css`) — see this repo's README for why it's a separate
// import rather than auto-injected.
import "./library.css";

export { PipelineApp } from "./PipelineApp";
export type { PipelineAppProps } from "./PipelineApp";
export type { WorkspaceRole } from "./types";
