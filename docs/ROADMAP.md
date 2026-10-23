# Roadmap

This roadmap describes generations of capability, not promises or deadlines.

## Generation 0 — manual experimentation

Status: historical.

- manually choose provider/model;
- compare outputs informally;
- learn provider behavior.

## Generation 1 — Jho Lite read-only

Status: **stable baseline frozen at v0.2.5**.

- candidate registry;
- Codex ACP;
- OpenCode ACP;
- AGY official headless;
- exact candidate identity;
- read-only policy;
- live progress;
- timeout/liveness;
- telemetry;
- immutable exports;
- Windows notification.

This baseline remains preserved on `main` / tag `v0.2.5`. New development occurs on feature branches.

## v0.3 — Adaptive Workflow Foundation

Status: **next active development generation**.

Goal: make the workflow, not the individual model, the primary orchestration unit.

Target capabilities:

- capability discovery and inventory snapshots;
- provider/model availability states;
- task contract and task profile;
- separate intrinsic complexity, historical difficulty, current empirical difficulty and risk;
- workflow templates;
- workflow instances with concrete role assignments;
- disposable Git worktree per task;
- one writer per worktree;
- planner / supplied-plan validation;
- edit-capable worker policy;
- deterministic validators;
- independent reviewer;
- optional security reviewer based on risk;
- bounded repair/rework;
- final verdict;
- workflow-level telemetry;
- no automatic push/merge/deploy.

### v0.3.0 initial milestone

Establish contracts before autonomous editing:

1. workflow optimization model and ADR;
2. capability inventory schema + discovery command;
3. task/workflow schemas;
4. workflow controller in dry-run mode;
5. deterministic routing from current priors;
6. only then introduce isolated edit execution.

## v0.4 — Empirical Workflow Learning

Target:

- task-family statistics;
- current empirical difficulty estimation;
- workflow effectiveness by context;
- first-pass success and rework models;
- duration/resource models;
- workflow champion/challenger;
- minimum sample and hysteresis rules;
- controlled experiments;
- drift detection.

## v0.5 — Adaptive Workflow Routing

Target:

- dynamic workflow selection;
- capability-aware role assignment;
- quota-aware routing;
- risk-sensitive exploration;
- Pareto-efficient workflow selection;
- automatic champion replacement when evidence supports it;
- newly discovered models introduced as probes/challengers.

## v1.0 — Adaptive Orchestrator

Target lifecycle:

```text
DISCOVER
  ↓
UNDERSTAND
  ↓
SELECT / DESIGN WORKFLOW
  ↓
ALLOCATE CURRENT RESOURCES
  ↓
EXECUTE
  ↓
VERIFY
  ↓
MEASURE
  ↓
LEARN
  ↓
ADAPT
```

Potential mature capabilities:

- scheduler;
- durable orchestration;
- richer policy engine;
- distributed execution;
- rich observability;
- statistical experiment service;
- predictive outcome models;
- contextual bandits when data volume and controls justify them.

## Important architectural rule

Jho Lite does not have to become the final serious orchestrator.

The v0.x line is allowed to prove contracts, collect evidence and expose failure modes. If evidence justifies a clean architecture, start a separate `jho-orchestrator` project and migrate only the proven ideas.

## Freeze rule

Do not add a feature merely because it is interesting. Add it when it unlocks real product work, protects experimental validity, or is required by the adaptive-workflow objective.
