# ADR-0008 — Optimize workflows, not individual models

Status: Accepted

## Context

Jho Lite originally measured model candidates as combinations of harness, provider, model, effort and role. That remains useful, but it is not the long-term optimization target.

A single powerful model can often solve a task, but the system should learn whether a coordinated workflow can achieve comparable or better quality with lower cost, lower latency, less quota consumption, lower failure probability or less human intervention.

Models and providers are transient resources. Workflow knowledge should survive model churn.

## Decision

The primary optimization unit is a **WorkflowCandidate**.

A workflow candidate may include:

- task profile and constraints;
- planner policy;
- worker policy;
- concrete model assignments per role;
- deterministic validators;
- reviewer policy;
- optional security reviewer;
- retry and rework policy;
- escalation policy;
- context strategy;
- timeout and resource budgets;
- human-approval boundaries.

Model candidates remain first-class resources inside a workflow, but no model is assumed to be globally optimal.

The system should distinguish:

1. **WorkflowTemplate** — durable strategy expressed in roles and capabilities.
2. **WorkflowInstance** — concrete assignment of currently available providers/models/efforts to that template.

Example:

```text
WorkflowTemplate
strong planner
→ efficient coding worker
→ deterministic validators
→ independent reviewer
→ bounded repair
```

may be instantiated today with one set of models and later with a different set without invalidating the template.

## Optimization objective

Jho should learn workflows that maximize task outcome quality and reliability while minimizing resource consumption, latency, rework and human intervention, subject to task risk, availability, quota, policy and budget constraints.

No single scalar score is required initially. Multi-objective comparison and Pareto-efficient workflows are acceptable and preferred over premature collapsing of all objectives into one metric.

## Difficulty and risk

Task difficulty is not permanent. Preserve separate dimensions:

- intrinsic complexity;
- historical difficulty;
- current empirical difficulty;
- risk.

Current empirical difficulty may decrease or increase as models, prompts, tools, validators, harnesses and repository quality change. Risk does not automatically decrease when capability improves.

## Capability discovery

The router must not assume yesterday's model inventory is valid today. Provider/model capability discovery is part of the workflow decision process.

The system must preserve historical model identities even after they become unavailable.

## Consequences

- telemetry must identify the complete workflow, not only the worker model;
- workflow templates and concrete assignments must be versioned separately;
- routing must be availability-aware;
- new models enter controlled exploration rather than becoming champions automatically;
- long-term learning focuses on workflow performance by task context;
- model benchmarks remain priors, not final routing truth.
