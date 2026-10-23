# ADR-0009 — Product-first, evidence-gated orchestrator evolution

Status: Accepted

## Context

The long-term vision for Jho is ambitious: optimize complete multi-agent workflows under quality, cost, latency, risk and availability constraints, while learning from telemetry over time.

That vision is valuable, but expanding orchestration infrastructure before shipping `client-product` would repeat a known failure mode: the supporting system becomes the main project and delays the product it was meant to accelerate.

At the same time, freezing Jho permanently at read-only v0.2.5 would leave a recurring manual gap between planning, implementation, deterministic validation and independent review.

## Decision

Jho v0.3 will implement only the minimum end-to-end workflow needed to support real product work safely:

```text
task
→ current capability inventory
→ deterministic workflow selection
→ disposable isolated worktree
→ one edit-capable worker
→ deterministic validators
→ independent reviewer
→ at most one bounded repair initially
→ terminal verdict
→ telemetry + notification
```

The first milestone is complete when this lifecycle successfully executes one real `client-product` task without editing the user's main checkout and without automatic push, merge or deploy.

After that milestone, orchestration infrastructure work stops by default and development returns to the bot.

## Evidence gate

Further Jho development requires evidence from real use. A new feature should be justified by at least one of:

- a recurring product task is blocked;
- manual orchestration is materially repetitive;
- telemetry shows a measurable quality/reliability/resource opportunity;
- a security or isolation gap requires remediation.

Data Science, adaptive routing, contextual bandits, multi-objective optimization and richer observability are deferred until sample size and data quality make them defensible.

## Post-product reassessment

After the bot reaches its target milestone, perform a structured reassessment using the accumulated telemetry before choosing the next architecture.

The reassessment must include a fresh open-source landscape review. The system is not required to remain custom-built. Future options may include adopting, extending or using an external orchestrator as a substrate if it satisfies most requirements with lower maintenance cost and acceptable security/extensibility.

Sunk cost is not a reason to continue custom development.

## Consequences

- `client-product` remains the primary delivery objective;
- Jho becomes a product-development tool and data collection instrument;
- v0.3 architecture must preserve enough telemetry for later statistical analysis;
- advanced long-term ideas remain documented but intentionally unimplemented;
- the orchestrator's future is decided from evidence and the future OSS ecosystem, not from current attachment to the implementation.
