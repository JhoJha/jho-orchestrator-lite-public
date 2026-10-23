# Vision and Mission

## Mission

Use multiple AI coding systems as measurable engineering resources rather than opaque assistants.

Jho exists to make multi-model software work:

- reproducible;
- observable;
- comparable;
- policy-controlled;
- safe enough to leave unattended for bounded tasks;
- adaptive to changing providers, models and empirical performance.

## North star

Build an **Adaptive Workflow Optimizer** for software-engineering tasks.

The primary question is not:

> Which model is best?

It is:

> Given this task, risk, stack, budget and the resources available today, which workflow has the best expected trade-off between quality, reliability, resource use, latency, rework and human intervention?

The system should learn how to combine planners, workers, deterministic validators, reviewers, security reviewers, retry policies and escalation rules. A single frontier model is a useful baseline or occasional high-value resource, not the default architecture.

## Workflow-centric view

```text
task
  ↓
capability discovery
  ↓
classification
  ↓
intrinsic complexity / historical difficulty / current empirical difficulty / risk
  ↓
workflow template
  ↓
role assignment from currently available candidates
  ↓
planner / worker
  ↓
deterministic validators
  ↓
reviewer
  ↓
security reviewer when required
  ↓
retry / repair / escalation
  ↓
final verdict
  ↓
telemetry
  ↓
learning
```

## Two levels of identity

### ModelCandidate

A concrete execution resource:

```text
harness/access path
+ provider
+ model
+ variant or effort
+ role
```

Different effort levels remain separate candidates because they can have different cost, latency, failure and correctness profiles.

### WorkflowCandidate

The primary optimization unit:

```text
task strategy
+ role structure
+ concrete model assignments
+ validators
+ retry/rework policy
+ escalation policy
+ context strategy
+ budgets
```

A durable `WorkflowTemplate` should be separable from a time-specific `WorkflowInstance` so workflow knowledge can survive model churn.

## Dynamic ecosystem

Never assume that yesterday's capabilities remain available today.

Providers, subscriptions, quotas, models, variants and harness behavior can change. Jho should maintain a current capability inventory and preserve historical model identities even after they disappear.

Likewise, never assume that historical task difficulty equals current difficulty. A task that is HIGH today may become MEDIUM or LOW as models, tools, validators and workflows improve. Risk remains independent from empirical difficulty.

## Evidence hierarchy

External benchmarks are useful as priors. Local evidence from real tasks becomes authoritative over time.

The expected evolution is:

```text
cold start
    ↓
controlled exploration
    ↓
statistical comparison
    ↓
workflow champion / challenger
    ↓
adaptive workflow routing
    ↓
continual reevaluation
```

The long-term asset is not allegiance to any specific model. It is accumulated knowledge about how to combine changing AI resources into effective engineering workflows.
