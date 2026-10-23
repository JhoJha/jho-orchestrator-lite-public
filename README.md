# Jho Orchestrator Lite

A small, safety-first, multi-harness AI orchestration layer created to help ship real software while collecting evidence for a future, more capable orchestrator.

> **Current status:** frozen at **v0.2.5** as a stable read-only orchestration tool.
>
> **Primary next use:** apply it to real work, especially `client-product`, and collect telemetry before expanding the orchestration architecture.

## Why this repository exists

Jho Lite is intentionally **not** the final orchestrator vision. It is the pragmatic bridge between manual multi-model work and a future empirical, adaptive orchestration system.

Today it provides a common logical contract over:

- Codex through ACP;
- OpenCode through ACP;
- Google Antigravity (AGY) through the official headless CLI;
- explicit model + effort/variant candidate identity;
- read-only safety controls;
- live progress and liveness;
- JSONL telemetry with trace/span identifiers;
- immutable Markdown run exports;
- Windows desktop notifications from WSL.

Start here:

1. [`docs/00_START_HERE.md`](docs/00_START_HERE.md)
2. [`docs/CURRENT_STATE.md`](docs/CURRENT_STATE.md)
3. [`docs/ROADMAP.md`](docs/ROADMAP.md)
4. [`docs/LONG_TERM_ORCHESTRATOR.md`](docs/LONG_TERM_ORCHESTRATOR.md)
5. [`docs/HANDOFF_CURRENT.md`](docs/HANDOFF_CURRENT.md)

## Important boundaries

- Jho Lite is currently **read-only** against the product repository.
- It does not auto-commit, push, merge, deploy or mutate production systems.
- GPT models are not routed through OpenCode.
- NVIDIA is background-long only.
- AGY uses the official CLI directly, never a third-party OAuth bridge.
- Chain-of-thought text is not stored or printed.
- A notification failure never changes the run result.

## Local model

The repo itself is the durable source of truth. At runtime, `scripts/bootstrap_local.sh` installs the files as a local overlay into the pinned protoAgent checkout.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).
