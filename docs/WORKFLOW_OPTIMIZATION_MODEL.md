# Workflow Optimization Model

## North star

Jho is not primarily a model router. Its long-term purpose is to learn the most effective **workflow strategy** for each task context using the resources that are actually available at execution time.

The central question is:

> Given this task, risk, stack, budget and current capability inventory, which workflow has the best expected trade-off between quality, reliability, resource use, latency, rework and human intervention?

## Core entities

### TaskProfile

A task profile should preserve at least:

- task family;
- stack/context;
- explicit acceptance criteria;
- constraints;
- intrinsic complexity;
- historical difficulty;
- current empirical difficulty;
- risk.

Do not collapse difficulty and risk into one label.

### ModelCandidate

A concrete execution resource:

```text
harness/access path
+ provider
+ model
+ variant/effort
+ role
```

### WorkflowTemplate

A model-independent orchestration strategy.

Example:

```text
planner(strong_reasoning)
→ worker(strong_coding, cost_efficient)
→ validators(deterministic)
→ reviewer(independent_provider)
→ repair(worker, bounded)
```

### WorkflowInstance

A concrete realization of a template using the current inventory.

Example:

```text
planner  = candidate A
worker   = candidate B
reviewer = candidate C
```

The same template can survive replacement of all three models.

## Capability discovery

Before routing, Jho needs an inventory snapshot with provider, harness and candidate state.

Possible states include:

- DISCOVERED_UNBENCHMARKED;
- AVAILABLE;
- DEGRADED;
- QUOTA_EXHAUSTED;
- AUTH_FAILED;
- UNAVAILABLE;
- REMOVED;
- UNKNOWN.

Discovery should run at minimum on first use of the day, when the inventory cache expires, and after provider/model failures that indicate stale state.

A missing model is never deleted from historical telemetry. It becomes unavailable/removed with first-seen and last-seen history.

Newly discovered providers or models do not become high-risk champions automatically. They begin as probe/shadow/challenger resources until evidence justifies promotion.

## Workflow selection

Early versions should use deterministic policy + priors + task metadata + availability.

Later versions can estimate quantities such as:

```text
P(final_success | workflow, task_context)
E(duration | workflow, task_context)
E(resource_use | workflow, task_context)
E(rework | workflow, task_context)
E(quality | workflow, task_context)
```

Workflow choice is multi-objective. A Pareto frontier is preferable to declaring one universal winner.

For example:

```text
WF-A: quality 94, resource index 20
WF-B: quality 96, resource index 45
WF-C: quality 98, resource index 90
```

The correct workflow depends on risk, quality floor, budget and urgency.

## Execution lifecycle

Target lifecycle for v0.3+:

```text
input task
  ↓
capability discovery / inventory snapshot
  ↓
task classification
  ↓
workflow template selection
  ↓
role assignment
  ↓
isolated worktree
  ↓
planner / plan validation
  ↓
worker
  ↓
deterministic validators
  ↓
reviewer
  ↓
bounded repair loop
  ↓
security reviewer when policy requires it
  ↓
final verdict
  ↓
telemetry + durable artifacts
```

No automatic push, merge or deploy in the initial edit-capable phase.

## Dynamic difficulty

Current empirical difficulty is allowed to drift.

A task family can move from HIGH to MEDIUM to LOW when recent evidence shows that current models and workflows solve it reliably with less effort. It can also move upward after regressions, ecosystem changes or harder observed instances.

Changes should require enough evidence and hysteresis so a few lucky or unlucky runs do not cause oscillation.

Risk is independent. A security-sensitive task can become empirically easy while remaining high-risk and still requiring independent review.

## Learning progression

```text
external benchmark priors
→ local telemetry
→ descriptive statistics
→ controlled workflow experiments
→ contextual champions/challengers
→ drift detection
→ predictive outcome models
→ adaptive workflow routing
```

Potential later methods include regression, Bayesian models, survival/time models, tree-based models, non-inferiority testing, Pareto optimization and contextual bandits.

## Research value

The dataset should support future analysis of:

- which workflow structures outperform single-model baselines;
- role-specific model strengths;
- quality/resource trade-offs;
- generalization of workflow templates across model churn;
- dynamic empirical difficulty;
- failure attribution;
- capability drift;
- whether orchestration can approach frontier-model quality at lower resource use.

This can support future academic work, but novelty must be established through a proper literature review rather than assumed.
