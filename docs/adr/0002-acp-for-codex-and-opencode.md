# ADR-0002 — Use ACP for Codex and OpenCode

Status: Accepted

## Decision

Use protoAgent ACP sessions for Codex and OpenCode and set candidate configuration before the first prompt.

## Consequences

The two harnesses share a common transport path while still preserving provider/model/effort identity.
