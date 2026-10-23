# ADR-0007 — Desktop notifications are best-effort

Status: Accepted

## Decision

Use WSL → Windows notifications for operator UX, but never let notification failure alter task success/failure.

## Consequences

The notification subsystem is observable but non-authoritative.
