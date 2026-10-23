# ADR-0001 — Use protoAgent as the initial substrate

Status: Accepted

## Context

The project needed orchestration/session plumbing quickly without spending product time rebuilding a general agent runtime.

## Decision

Pin protoAgent and build Jho-owned behavior as a local overlay.

## Consequences

- faster start;
- upstream remains inspectable;
- dependency pinning is mandatory;
- future replacement remains possible.
