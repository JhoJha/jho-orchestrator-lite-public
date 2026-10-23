# Long-Term Orchestrator

This document preserves the larger vision while the v0.x line proves the contracts with real product work.

## Optimization target

The system should optimize **complete workflow configurations**, not individual models.

A candidate workflow can vary:

- planner strategy;
- worker strategy;
- provider/access path per role;
- model per role;
- effort/variant per role;
- deterministic validators;
- reviewer;
- security reviewer;
- retry/rework budget;
- escalation rules;
- context strategy;
- tool policy;
- timeout;
- resource budget;
- human-approval boundaries.

A model candidate is a resource inside a workflow. The workflow is the primary unit of optimization.

## Durable workflow knowledge

Separate:

### WorkflowTemplate

A strategy described by roles, capability requirements, validators and control policy.

### WorkflowInstance

A concrete assignment of the providers/models/efforts that are currently available.

This distinction allows learned process knowledge to survive model retirement, subscription changes and provider churn.

## Capability inventory

Never assume yesterday's provider/model inventory is valid today.

The orchestrator should refresh a capability snapshot on first use of the day, after a bounded cache TTL and when execution failures suggest stale inventory.

Track candidate/provider states such as:

- discovered/unbenchmarked;
- available;
- degraded;
- quota exhausted;
- auth failed;
- unavailable;
- removed;
- unknown.

Preserve historical identities after removal so old runs remain interpretable.

Newly discovered resources begin as probes/challengers rather than high-risk champions.

## Difficulty dimensions

Do not collapse these into one score.

### Intrinsic Complexity

A priori structural complexity of the task itself.

### Historical Difficulty

How difficult similar tasks have been over time.

### Current Empirical Difficulty

How difficult the task family is for the **current** ecosystem of models, prompts, harnesses, validators, tools and workflow policies.

### Risk

Potential impact of a wrong action or wrong answer.

These values can move independently.

## Why empirical difficulty must drift

A task that is hard today may become easy later because:

- models improve;
- effort modes change;
- prompts improve;
- tools improve;
- validators improve;
- repository structure improves;
- harnesses improve;
- workflow strategy improves.

The reverse can also happen after ecosystem regressions or task-distribution changes.

Difficulty transitions should eventually use enough recent evidence, uncertainty estimates and hysteresis rather than reacting to a handful of runs.

Risk remains independent: an empirically easy authentication change may still be high-risk and require stronger review.

## Workflow champion/challenger

There should not be one global champion model or one global champion workflow.

Champions can be contextual:

```text
(task_family, stack, risk, empirical_difficulty, available_resources, budget)
```

A challenger receives controlled traffic to detect capability/workflow changes without taking excessive risk.

## Exploration

Exploration budget should be risk-sensitive.

- low-risk tasks: more workflow/model experimentation;
- medium-risk tasks: bounded challengers;
- high-risk tasks: conservative workflow + independent review;
- critical tasks: strong deterministic policy + explicit human integration boundary.

## Multi-objective optimization

The router should reason about a vector of outcomes rather than prematurely reducing everything to one number:

- quality/correctness;
- reliability;
- first-pass success;
- final success;
- latency;
- resource consumption;
- rework;
- human intervention;
- risk exposure.

Different Pareto-efficient workflows may be correct for different budgets and risk profiles.

## Failure attribution

A failed workflow should attempt to separate:

- task/plan ambiguity;
- model reasoning failure;
- planner failure;
- worker failure;
- harness failure;
- provider quota;
- provider outage;
- permission block;
- configuration mismatch;
- validator failure;
- reviewer rejection;
- security-review rejection;
- repository/environment failure;
- user/policy constraint.

Without attribution, workflow performance statistics become misleading.

## Learning path

```text
external priors
→ deterministic workflow rules
→ local telemetry
→ descriptive statistics
→ controlled workflow experiments
→ contextual champion/challenger
→ predictive outcome models
→ adaptive workflow routing
→ continual reevaluation
```

The long-term system may use statistical learning or contextual bandits, but only after measurement quality and outcome labels are trustworthy.
