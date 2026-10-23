# AGENTS.md

## Project intent

This repository preserves the current Jho Orchestrator Lite and the knowledge needed to resume it later.

## Safety rules

- Do not commit secrets, tokens, credentials, `.env`, provider settings or run telemetry.
- Do not weaken read-only safety in the main product checkout.
- Do not add automatic push/merge/deploy without an explicit architecture decision.
- Do not store or print chain-of-thought.
- Do not route GPT through OpenCode.
- Do not use NVIDIA in the interactive critical path unless RULE-003 is explicitly changed.
- Use the official AGY CLI; do not introduce third-party Antigravity credential bridges.
- Keep protoAgent pin changes deliberate and documented.

## Scope discipline

Before adding infrastructure, explain which real product workflow it unlocks and why the current Lite cannot handle it.

Read `docs/00_START_HERE.md` before major changes.
