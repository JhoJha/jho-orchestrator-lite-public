# Current Handoff

**Stable baseline:** v0.2.5

**Active development branch:** `feature/adaptive-workflow-v0.3`

## Current status

Jho Lite v0.2.5 remains the stable **read-only** baseline for analysis/review.

The active v0.3 branch is intentionally narrow: build one safe end-to-end orchestration loop that can advance `client-product`, then stop infrastructure work and return to the product.

## Long-term direction

The project has rotated from "find the best model" to **optimize the best workflow for each task context**.

The primary future optimization unit is a `WorkflowCandidate`, not an individual model.

The long-term system should learn which combination of planner, worker, validators, reviewer, security reviewer, retry/rework policy, context strategy and resource assignment gives the best trade-off between quality, reliability, latency, resource use and human intervention.

Models/providers are replaceable resources inside those workflows. Workflow knowledge should survive model churn.

See:

- `VISION_AND_MISSION.md`
- `WORKFLOW_OPTIMIZATION_MODEL.md`
- `LONG_TERM_ORCHESTRATOR.md`
- `adr/0008-optimize-workflows-not-models.md`

## Task difficulty model

Do not collapse everything into one LOW/MEDIUM/HIGH score.

Preserve separately:

- intrinsic complexity;
- historical difficulty;
- current empirical difficulty;
- risk.

A task may become empirically easier as models, prompts, tools, validators and harnesses improve while its risk remains high.

## Capability inventory principle

Never assume yesterday's model/provider inventory is valid today.

The intended v0.3 foundation should refresh currently available providers/models at least on first use of the day, on cache expiry, or when a provider/model failure suggests stale state.

Preserve historical model identities even when they disappear.

New providers/models begin as discovered/probe/challenger resources rather than automatic champions.

## Minimum v0.3 target

```text
TASK.md
  ↓
capability refresh
  ↓
deterministic workflow selection from policy + priors
  ↓
disposable Git worktree
  ↓
one edit-capable worker
  ↓
deterministic validators
  ↓
independent reviewer
  ↓
at most one bounded repair initially
  ↓
READY_FOR_HUMAN_REVIEW / NEEDS_HUMAN
  ↓
telemetry + Windows notification
```

## Safety boundaries

- never edit the main product checkout;
- one writer per worktree;
- no automatic push;
- no automatic merge;
- no automatic deploy;
- no production credentials;
- bounded retries/rework;
- deterministic checks are decided by deterministic validators;
- reviewer/provider independence when practical;
- no chain-of-thought persistence.

## Stop rule

Once the minimum lifecycle above successfully completes **one real `client-product` task end-to-end**, stop Jho infrastructure work by default and return to the bot.

During the bot phase, prefer roughly 80–90% product work and 10–20% orchestrator maintenance/improvement.

New Jho features require evidence from real product use: a recurring blocker, repeated manual orchestration, a measurable quality/reliability/resource opportunity, or a security/isolation gap.

See `V0_3_PRODUCT_FIRST_SCOPE.md` and `adr/0009-product-first-evidence-gated-evolution.md`.

## Telemetry objective

The bot phase is also an evidence-collection phase.

Preserve enough data to later analyze workflow performance by task family, stack, difficulty, risk, inventory state, role/model/effort assignments, validators, attempts, reworks, duration, first-pass/final success, reviewer findings, failure attribution and human intervention.

Do not equate subscription access with zero economic cost.

## After the bot

Before expanding the orchestrator substantially:

1. analyze the accumulated telemetry;
2. identify which workflow stages actually added value;
3. estimate whether there is enough data for statistical/DS methods;
4. repeat a fresh open-source landscape review;
5. decide whether to continue custom development, adopt/extend another project, use another substrate, or migrate components.

Do not protect sunk cost. If future open-source software solves most of the problem safely and extensibly, using it may be better than continuing custom development.

Potential later research areas include statistical workflow comparison, non-inferiority vs strong-model baselines, dynamic empirical difficulty, drift detection, Pareto analysis, champion/challenger experiments and contextual bandits.

## Immediate engineering next step

Implement the smallest reliable capability inventory/discovery layer needed by the minimum v0.3 workflow, then isolated edit execution, validators and independent review.

Do not implement advanced adaptive routing before real product runs exist.
