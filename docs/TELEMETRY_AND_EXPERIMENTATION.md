# Telemetry and Experimentation

## Purpose

Telemetry exists to make orchestration decisions evidence-based and to preserve enough context for future workflow optimization, drift analysis and possible academic research.

The primary analytical unit is evolving from a single model run to a **workflow execution** composed of one or more role-specific model runs plus deterministic validators.

## Current sink

Local JSONL:

```text
local_jho/telemetry/runs.jsonl
```

Run IDs use trace-style identifiers and each run produces durable artifacts.

## Model-run measurements

Capture or derive, where available:

- task id and workflow id;
- task family/class;
- stack/context;
- risk;
- intrinsic complexity;
- historical difficulty;
- current empirical difficulty estimate;
- provider/harness;
- model;
- effort/variant;
- role;
- duration;
- tool activity;
- first-pass success;
- final success;
- retry/rework count;
- tests/validator result;
- reviewer findings;
- failure category;
- failure attribution;
- token/context indicators;
- quota before/after where available;
- cost only when a defensible cost model exists.

Do **not** equate “included in a subscription” with zero economic cost.

## Workflow-level measurements

A workflow execution should additionally preserve:

- `workflow_template_id`;
- `workflow_template_version`;
- `workflow_instance_id`;
- planner candidate;
- worker candidate;
- reviewer candidate;
- security-review candidate when used;
- validators executed and their raw pass/fail summaries;
- worker attempts;
- review-rework cycles;
- escalation events;
- total duration;
- total resource indicators;
- human intervention count/type;
- final verdict;
- quality score or evaluation outcome when one can be defined defensibly;
- predicted outcome when the router makes a prediction;
- actual outcome;
- `inventory_snapshot_id` describing the alternatives available at routing time.

The inventory snapshot is essential. A workflow should not be penalized for failing to select a model that was unavailable, unauthenticated or quota-exhausted at that time.

## Capability and harness history

Preserve enough metadata to reconstruct the environment that produced a result:

- provider availability state;
- first/last seen model timestamps;
- model removed/unavailable state;
- harness/CLI version;
- protoAgent version/commit;
- Jho runner version;
- routing policy version;
- workflow template version.

Do not delete historical model identities when providers retire them.

## Chain of thought

Do not persist chain-of-thought text.

Activity counters and tool/liveness events are sufficient for observability.

## Experimental progression

```text
external benchmark priors
→ controlled local exploration
→ enough samples per task family
→ descriptive statistics
→ controlled workflow comparison
→ contextual workflow champion/challenger
→ drift detection
→ predictive outcome models
→ adaptive routing
→ continual reevaluation
```

## Dynamic empirical difficulty

Do not treat task difficulty as permanent.

Current empirical difficulty should eventually be inferred from recent evidence such as first-pass success, attempts, validator failures, reviewer rework, duration and human intervention for comparable tasks.

Difficulty changes should use minimum sample sizes, confidence/uncertainty and hysteresis to avoid oscillation from small samples.

Risk remains a separate dimension and should not automatically decrease when empirical difficulty decreases.

## Multi-objective evaluation

Do not prematurely reduce workflow quality to a single score.

Useful outcome dimensions include:

- task correctness / quality;
- reliability;
- first-pass success;
- final success;
- latency;
- resource use;
- rework;
- human intervention;
- risk exposure.

Pareto-efficient workflows may coexist for different budgets and risk profiles.

## Future statistical methods

When data volume and measurement quality justify them, consider:

- regression / logistic regression;
- hierarchical or Bayesian models;
- time-to-completion models;
- tree-based predictive models;
- non-inferiority testing against strong single-model baselines;
- drift/change-point analysis;
- Pareto optimization;
- contextual bandits for controlled online exploration.

Methods are secondary to measurement quality. Do not introduce a complex learner before outcome labels and failure attribution are trustworthy.

## Future data architecture

When there is enough volume, consider:

- PostgreSQL for analytical run/workflow history;
- OpenTelemetry for execution traces;
- Prometheus/Grafana for aggregate operational metrics.

Do not add them merely because they are available.
