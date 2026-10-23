# Routing Strategy

## Current state

Routing is still primarily explicit/manual. `routing_policy_v1.json` stores priors and intended policy, not a mature autonomous router.

## Hard rules

### RULE-001 — Never Astra

`gpt-6-astra` is excluded unless the policy is explicitly reversed.

### RULE-002 — GPT via Codex only

Do not spend OpenCode capacity on GPT when direct Codex access exists.

### RULE-003 — NVIDIA background-long only

Keep high-latency NIM work away from the interactive path.

### RULE-004 — Effort is part of identity

`model@high` and `model@max` are separate candidates.

### RULE-005 — Harness parity

Codex, OpenCode and AGY should expose the same logical lifecycle even when the transport differs.

## Future router inputs

A future router may use:

- task family;
- stack;
- intrinsic complexity;
- historical difficulty;
- current empirical difficulty;
- risk;
- role;
- candidate availability;
- remaining quota;
- latency;
- first-pass success;
- rework rate;
- validator results;
- reviewer findings;
- recent drift.

## External benchmark rule

External benchmarks initialize priors.

Local task telemetry gradually becomes more important.
