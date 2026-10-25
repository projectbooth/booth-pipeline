# Shared components booth-pipeline needs from booth-design

Status: **requested**, 2026-10-25. From the user's pipelines-area UX feedback (booth-architecture
`agent-briefs/ux-feedback-2026-10-24.md`), worked in booth-pipeline PR #25. booth-pipeline didn't build
private copies of these; it uses them once booth-design publishes them.

## Why this note exists

booth-design publishes no shared component package today: its `package.json` is `@booth/design`, `private`,
and its Button, Field, DataTable and others live inside the shell app. A native module's UI package can't
import them, because the dependency direction is shell → module (ADR 0030). booth-pipeline has used local
primitives on the shared Tailwind slate and indigo palette since v0, as booth-catalog, booth-storage and
booth-module-store do. This repo's `docs/decisions/0004-ui-component-library.md` first raised the gap.

## 1. Published package (the prerequisite)

- An npm package modules can depend on, for example `@projectbooth/design-ui`: tokens plus the components
  below. It should export ESM with type declarations and keep React as a peer dependency, like the module UI
  packages.
- **Theme:** components follow the shell's theme (`data-theme`, light and dark) with no setup in the module.
- **Styles:** the shell already rebuilds Tailwind from every module's `dist/**/*.js`. The package must either
  work under that build or ship its own stylesheet, and its README should say which.

## 2. Sticky action bar

**What it's for:** a form's save actions stay on screen however far the form is scrolled. Examples are "Save
schedule" and "Save" / "Save as new version…" on the task editor. In PR #25 these moved above the form; on a
long form scrolled far down they still scroll out of view.

It should:
- **Stay in reach:** pinned to the bottom of the module's content area (not the window: the shell has its own
  header and sidebar). It stays inside its form, so two forms on one page don't share a bar.
- **Hold the actions:** a primary action, secondary actions, and a status slot. The status slot shows
  "Unsaved changes", "No changes yet", "Saving…", or an error with an optional link to the field at fault.
- **Never hide content:** it doesn't cover the form's last field; the page gets bottom padding equal to the
  bar.
- **Work from the keyboard:** reachable in tab order after the form's fields. Optionally, Ctrl/Cmd+S runs the
  primary action, but only when the module opts in.
- **Fit narrow screens:** below 640 px the actions wrap and the status sits above them.

## 3. Collapsible panel / section

**What it's for:**
- the pipeline list's group headings (today plain buttons in a table row);
- panels on the pipeline page that are worth hiding, such as the summary strip and the run's trigger events.

It should:
- **Have a heading row:** a title, an optional count or badge, optional actions on the right, and a
  chevron.
- **Behave as a real disclosure:** a `button` with `aria-expanded` and `aria-controls`, toggled with
  Enter/Space.
- **Remember its state (optional):** a key, stored per viewer by the component, tolerating unavailable storage
  (private windows). The default state is the module's choice.
- **Fit inside a table:** a variant that sits in a list or table as a group header (colSpan row), so grouped
  lists don't each build their own.
- **Not move other content:** opening and closing must not change the size of anything else in a way that makes
  React Flow refit. booth-pipeline keeps its DAG canvas at fixed pixel heights for this reason (`DagCanvas.tsx`).

## 4. Tooltip and contextual help

This is the pattern from the UX brief, item 1: a "?" affordance on labels, column headers and status chips,
with short plain-language text.

It should:
- **Work from the keyboard and screen readers:** opens on focus as well as hover, closes on Escape, and the text
  reaches screen readers (`aria-describedby`), not only sighted mouse users.
- **Take text from the module:** plain text, or a small amount of inline markup. It optionally links to the
  module's docs page once the "Docs" sidebar entry exists.
- **Stay inside the module:** it never overflows the module's content area. It flips or shifts at the edges of
  the iframe or pane.

## What booth-pipeline does until then

- Save actions sit above the form, with an unsaved-state badge.
- The pipeline list's group headings are buttons with `aria-expanded`.
- Nothing else here is collapsible.

When the package exists, booth-pipeline replaces these with the shared components and drops the matching local
primitives.
