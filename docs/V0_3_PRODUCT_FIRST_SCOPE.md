# v0.3 Product-First Scope

## Purpose

Jho v0.3 is not an attempt to finish the long-term adaptive orchestrator before `client-product` ships.

Its purpose is to leave behind a **small, functional orchestration runtime with good architectural foundations** that can be used on real product work and can collect evidence for a later decision.

The bot is the product. Jho is the tool that helps build it.

## Immediate outcome

The first useful v0.3 milestone is:

```text
TASK.md
  ↓
capability refresh
  ↓
workflow selection from deterministic policy + priors
  ↓
isolated disposable Git worktree
  ↓
worker
  ↓
deterministic validators
  ↓
independent reviewer
  ↓
optional bounded repair (maximum 1 initially)
  ↓
final verdict
  ↓
telemetry + Windows notification
```

A successful task should finish as `READY_FOR_HUMAN_REVIEW` or `NEEDS_HUMAN`.

## Safety boundary

Initial v0.3 must keep these boundaries:

- never edit the user's main product checkout;
- one writer per disposable worktree;
- no automatic push;
- no automatic merge;
- no automatic deploy;
- no production credentials;
- deterministic validators are authoritative for deterministic checks;
- reviewer should be independent from the worker when practical;
- retry/rework loops are bounded;
- failures must terminate rather than loop indefinitely.

## Capability discovery: minimum viable version

Do not build a universal provider-discovery platform now.

For the providers already used by Jho, collect an inventory snapshot on first use of the day, when the cache expires, or after a provider/model failure that suggests stale state.

The snapshot should preserve, where available:

- harness/provider/access path;
- authentication/health state;
- models currently exposed;
- supported effort/variant values when discoverable;
- CLI/harness version;
- first_seen / last_seen;
- availability state;
- discovery timestamp.

History must be append-only or otherwise preserved. A disappeared model becomes unavailable/removed; historical runs must remain interpretable.

## Routing in v0.3

Routing is deliberately simple at first.

Use:

```text
hard policy
+ current availability
+ task metadata
+ external priors
+ early local evidence
```

Do not implement statistical learning, contextual bandits, Pareto optimization or automatic champion replacement until real data justifies them.

The long-term optimization unit remains the complete workflow, not the individual model.

## Telemetry to preserve now

Each workflow execution should make future analysis possible. Preserve at least:

- task_id;
- task_family;
- stack/context;
- acceptance criteria reference;
- intrinsic_complexity;
- historical_difficulty if known;
- current_empirical_difficulty if estimated;
- risk;
- inventory_snapshot_id;
- workflow_template_id/version;
- workflow_instance_id;
- role assignments;
- provider/model/effort per role;
- validators and results;
- attempts and reworks;
- duration;
- token/context/quota signals where defensible;
- cost estimate only when defensible;
- first_pass_success;
- final_success;
- reviewer findings;
- failure_category;
- failure_attribution;
- human intervention required;
- final verdict.

Do not store chain-of-thought text.

## Stop rule

Once Jho can complete one real `client-product` task end-to-end through the minimum lifecycle above, stop infrastructure work and return to the product.

New Jho features are allowed only when one of these is true:

1. a real bot task is blocked by a missing capability;
2. repeated manual orchestration is materially wasting time;
3. collected evidence shows that a specific change is likely to improve quality, reliability or resource use;
4. a security/isolation gap must be fixed before further autonomous use.

## Product-first operating model

During the bot phase, prefer roughly:

```text
80–90% product work
10–20% orchestrator maintenance/improvement
```

The ratio is a guardrail, not a metric to optimize.

The intended loop is:

```text
bot needs work
  ↓
Jho executes or assists
  ↓
bot advances
  ↓
telemetry accumulates
  ↓
real limitation appears
  ↓
small Jho improvement
  ↓
back to bot
```

## Post-bot decision gate

When the client product reaches the agreed product milestone, pause before expanding the orchestrator.

Perform a formal reassessment using the accumulated data.

Questions to answer:

- Did multi-role workflows materially improve quality or reduce rework?
- Which task families benefited from planning, reviewing or security review?
- Which stages added little value?
- Did model/provider diversity improve outcomes?
- Which workflow templates were Pareto-efficient in quality, time and resource use?
- How stable were results across model/provider churn?
- Is there enough data to justify empirical routing or Data Science?
- Is Jho Lite still the correct substrate?

## Open-source re-evaluation gate

Before building the next major generation, repeat an open-source landscape review.

Do not assume the best decision in 2026 is still the best decision later.

Evaluate whether an external project can provide most of the required capabilities:

- multi-provider/model execution;
- role/workflow graphs;
- durable task state;
- isolated workspaces/worktrees or equivalent;
- deterministic validator integration;
- independent review loops;
- capability discovery;
- policy/risk controls;
- telemetry hooks;
- model/effort identity;
- bounded retries/escalation;
- extensible routing;
- provider churn handling.

For each candidate, classify the choice as one of:

```text
ADOPT
ADOPT + EXTEND
USE AS SUBSTRATE
BORROW COMPONENTS
REJECT
CONTINUE CUSTOM
```

The goal is not to protect sunk cost. If an open-source system later solves 70–90% of the problem safely and extensibly, migrating may be better than continuing custom development.

## Long-term research path

If the operational dataset becomes large and clean enough, the next research layer may include:

- statistical workflow comparison;
- non-inferiority analysis against strong-model baselines;
- task-family performance models;
- dynamic empirical difficulty;
- drift detection;
- multi-objective/Pareto analysis;
- champion/challenger experiments;
- contextual bandits or other adaptive routing methods.

This path is intentionally deferred until there is enough real evidence.
