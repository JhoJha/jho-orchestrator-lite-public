# ADR-0003 — Integrate AGY through the official headless CLI

Status: Accepted

## Decision

Use the official `agy` CLI directly with `stream-json`, model selection, sandboxing and headless execution.

Do not use third-party Antigravity OAuth bridges.

## Consequences

AGY has a different transport implementation but must satisfy the same logical runner contract.
