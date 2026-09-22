# 0004: The brief says "built on booth-design's component library" — it isn't consumable yet

Status: **flag to the coordinator / `booth-design`.** Followed the fleet precedent meanwhile; nothing blocks.

The brief's v0 item: *a node-graph canvas, built on `booth-design`'s component library*. As of 2026-09-21
`booth-design`'s `package.json` is `"name": "@booth/design", "private": true` and exports no library: its Button,
Field, DataTable etc. live inside the shell app and cannot be imported by a module's UI package (ADR 0030 makes each
native module ship its own npm package, and the dependency direction is shell → module, never the reverse).

`booth-catalog`, `booth-storage` and `booth-module-store` each solved this the same way: small local primitives on the
shared Tailwind slate/indigo palette, keyed off `data-theme` (`components/ui.tsx`). **We did the same**, so the four
native modules look alike in the shell. Consequence: the primitives are duplicated four times and will drift from
`booth-design`'s tokens (which are OKLCH CSS variables, not the Tailwind palette).

**Ask:** either publish `@projectbooth/design-ui` (tokens + primitives) so modules can depend on it, or confirm the
duplicated-primitives approach is the intended v0 answer.

Additional dependency: **`@xyflow/react` (React Flow, MIT)** for the canvas, as ADR 0010 suggests. `booth-design` does
not provide it, so it is bundled into `@projectbooth/pipeline-ui` (~91 kB gzipped total) and its stylesheet ships in
`dist/style.css`. React Flow's attribution is left visible (hiding it is a paid option). `react`/`react-dom` are peer
dependencies, as in the other packages.
