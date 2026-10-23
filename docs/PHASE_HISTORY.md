# Phase History

## Phase 1 — registry and local overlay

- candidate registry;
- routing priors;
- telemetry sink;
- pinned protoAgent inspection.

## Phase 2 — ACP configuration proof

Validated that Codex and OpenCode expose selectable model and effort configuration before prompting.

## Phase 3 — runner v0.1

### v0.1.0

- first explicit read-only candidate run;
- telemetry;
- product integrity checks.

### v0.1.1

- live progress;
- heartbeat;
- tool breadcrumbs;
- stall warning;
- no chain-of-thought text.

### v0.1.2

- immutable Markdown run history;
- retained `JHO_LITE_LAST_RUN.md` convenience copy.

## Phase 4 — AGY parity

### v0.2.0

- official AGY headless adapter;
- stream-json parsing;
- model/effort mapping;
- sandbox;
- normalized permission failures.

### v0.2.1

- fixed AGY lifecycle initialization order;
- dynamic wire-model resolution;
- terminal adapter-error normalization.

### v0.2.2

- scoped product read permission.

### v0.2.3

- workspace confinement;
- child `PWD` alignment;
- outside-workspace detection;
- init-cwd validation.

## Phase 5 — desktop notification

### v0.2.4

Initial BurntToast integration.

### v0.2.5

- zero-install Windows NotifyIcon fallback;
- no admin/NuGet requirement;
- `jho-lite notify-test`;
- per-run `--no-notify`.

## Phase 6 — preservation

Repository consolidation, knowledge preservation, reproducible bootstrap and GitHub-ready structure.

No runtime feature is added in this phase.
