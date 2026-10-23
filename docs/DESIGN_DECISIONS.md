# Design Decisions

This file summarizes the most important choices. Formal ADRs are in `docs/adr/`.

## Use protoAgent as substrate

Reason: avoid rebuilding session/harness plumbing while preserving the option to replace the substrate later.

## Keep the Jho overlay out of upstream protoAgent history

Reason: upstream remains pinned and inspectable; local logic stays clearly owned by this project.

## ACP for Codex and OpenCode

Reason: both expose a workable configuration/session seam through protoAgent.

## Direct official CLI for AGY

Reason: official headless behavior is available and avoids unsupported third-party authentication bridges.

## Read-only first

Reason: prove configuration, telemetry, permissions and lifecycle before allowing automated edits.

## Exact candidate identity

Reason: model name alone is insufficient for meaningful comparison.

## Exit code is not verdict

Reason: provider/harness behavior can report process success while a required tool was denied.

## Notification is non-authoritative

Reason: desktop UX must never change the scientific/engineering result of a run.
