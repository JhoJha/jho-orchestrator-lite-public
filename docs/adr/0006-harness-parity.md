# ADR-0006 — Logical harness parity

Status: Accepted

## Decision

Codex, OpenCode and AGY must expose the same orchestration lifecycle:

- candidate;
- configuration;
- policy;
- run;
- progress;
- timeout;
- telemetry;
- terminal verdict.

Transport differences remain implementation details.
