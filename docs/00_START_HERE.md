# 00 — Start Here

If you are returning to this project after weeks, months or years, read this file first.

## One-sentence description

**Jho Orchestrator Lite is a deliberately small multi-harness orchestration layer built to coordinate AI coding agents safely, measure their real performance, and create evidence for a future adaptive orchestrator.**

## Why it was created

The original problem was not simply “which LLM is best?”. The real problem was how to use several providers, models, effort levels, roles, validators and reviewers without relying on intuition every time.

At the same time, there was a real product to ship: `client-product`. Building the full orchestration platform first would have delayed the product and produced architecture without enough empirical evidence.

Jho Lite therefore became the compromise:

```text
ship product now
      +
run several AI harnesses safely
      +
collect comparable telemetry
      +
learn from real tasks
      =
evidence for the future orchestrator
```

## Do not confuse these two systems

### Jho Lite

The current lightweight implementation. It should stay understandable, conservative and easy to operate.

### Long-term Jho Orchestrator

The future system described in `LONG_TERM_ORCHESTRATOR.md`: adaptive routing, champion/challenger, workflow optimization, validators, reviewers, security reviewers, quota awareness, drift detection and continual reevaluation.

Jho Lite does **not** need to grow endlessly until it becomes that system. It may eventually be replaced.

## Current version

`v0.2.5`

Current capabilities:

- Codex ACP;
- OpenCode ACP;
- AGY official headless CLI;
- exact model/effort selection where the harness exposes it;
- read-only execution;
- live terminal progress;
- timeout and liveness;
- JSONL telemetry;
- immutable Markdown exports;
- WSL → Windows completion notifications.

## Why development was paused here

The orchestration layer reached the minimum useful platform for real work. More infrastructure now has diminishing value compared with applying the tool to the product and gathering real execution data.

The rule at freeze time was:

> **Do not add a Jho Lite feature unless it directly unlocks a real product task or the accumulated evidence clearly justifies the next orchestration phase.**

## Resume sequence

When returning:

1. Read `CURRENT_STATE.md`.
2. Run `scripts/doctor.sh`.
3. Review `HANDOFF_CURRENT.md`.
4. Inspect recent telemetry, if available locally.
5. Re-check provider/model inventories because they drift.
6. Decide whether the evidence justifies continuing Jho Lite or starting the serious orchestrator.
