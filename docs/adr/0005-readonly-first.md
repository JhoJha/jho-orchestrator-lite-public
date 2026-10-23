# ADR-0005 — Read-only before automated edits

Status: Accepted

## Decision

The first stable phase may inspect and reason about the product but must not mutate the main checkout.

## Consequences

Repository-integrity checks are simple and strong. Edit automation is deferred to isolated worktrees.
