# Architecture

## Current runtime substrate

```text
                       JHO LITE
                          │
          ┌───────────────┼────────────────┐
          │               │                │
          ▼               ▼                ▼
       CODEX          OPENCODE            AGY
        ACP              ACP        official headless CLI
          │               │             stream-json
          └───────────────┴────────────────┘
                          │
                          ▼
                 common logical contract
```

The internal transport can differ. The logical contract should not.

## Harness contract

Every supported harness should expose:

1. explicit candidate identity;
2. model and effort/variant control where possible;
3. permissions fixed before execution;
4. non-interactive execution;
5. liveness/progress;
6. timeout;
7. normalized terminal result;
8. repository-integrity verification;
9. telemetry;
10. durable run output.

## v0.3 target architecture

The next layer sits above the harness contract.

```text
                    JHO CONTROLLER
                          │
         ┌────────────────┼────────────────┐
         │                │                │
         ▼                ▼                ▼
 CAPABILITY          TASK PROFILE      POLICY ENGINE
 DISCOVERY               │                │
         │                │                │
         └────────────────┼────────────────┘
                          ▼
                  WORKFLOW ROUTER
                          │
                          ▼
                WORKFLOW INSTANCE
                          │
         ┌────────────────┼─────────────────┐
         ▼                ▼                 ▼
      PLANNER           WORKER           REVIEWER
                          │                 │
                          ▼                 │
                    VALIDATORS             │
                          │                 │
                          └────────┬────────┘
                                   ▼
                         REPAIR / ESCALATE
                                   │
                                   ▼
                       SECURITY REVIEWER
                         when policy requires
                                   │
                                   ▼
                           FINAL VERDICT
                                   │
                                   ▼
                     TELEMETRY / LEARNING
```

## Capability discovery

Routing must operate on a time-specific inventory snapshot rather than a permanently trusted static registry.

The static registry remains useful for policy, aliases, known capabilities and hard routing restrictions. Discovery determines what is currently usable.

The discovery layer should capture:

- harness installed/version;
- provider/auth reachability where safely detectable;
- models currently exposed;
- effort/variant options where discoverable;
- provider/candidate availability state;
- quota indicators where a provider exposes them safely;
- first-seen/last-seen changes;
- newly discovered and removed candidates.

Discovery should be cached with a bounded TTL and refreshed on first use of the day and on stale-inventory failures.

## Workflow representation

Separate durable strategy from concrete resource assignment.

```text
WorkflowTemplate
  roles + capability requirements
  validators
  retry/rework policy
  escalation policy
  budgets

        ↓ instantiate against current inventory

WorkflowInstance
  planner candidate
  worker candidate
  reviewer candidate
  security reviewer candidate
  concrete limits
  inventory_snapshot_id
```

This separation allows workflow knowledge to survive model/provider churn.

## Task representation

A task contract should include objective, acceptance criteria, constraints and context. Classification should preserve separate dimensions for:

- intrinsic complexity;
- historical difficulty;
- current empirical difficulty;
- risk.

A supplied user/architect plan can be validated rather than regenerated. Vague tasks can invoke a planner.

## Controlled edit execution

The v0.2.5 main checkout integrity invariant remains valid.

Edit-capable execution must occur in disposable isolated Git worktrees:

```text
source repository checkout
        │
        └── create disposable worktree
                │
                ├── one writer
                ├── deterministic validators
                ├── independent reviewer
                └── bounded repair
```

The initial autonomous workflow must not automatically push, merge or deploy. Human approval remains the final integration boundary.

## Deterministic validators

LLMs do not decide whether deterministic commands passed.

Where applicable, the controller executes repository-defined validation commands such as tests, lint, type checking, build, migration checks or security checks and records their real exit status and evidence.

## Routing and learning boundary

v0.3 routing starts deterministic: policy + priors + task metadata + current availability.

Later generations may use statistical estimates and champion/challenger exploration, but the execution controller and safety policy remain deterministic boundaries around learned decisions.

## protoAgent role

protoAgent remains an OSS orchestration substrate, not the product and not the final orchestrator itself.

The runtime checkout stays pinned. Jho-specific logic is installed as a local overlay so upstream remains clean.

## ACP path

Codex and OpenCode use protoAgent `AcpClient`.

The current runner configures the candidate before the first prompt and uses host-side permission enforcement.

## AGY path

AGY intentionally does **not** use a third-party ACP/OAuth bridge.

```text
Jho Lite
  ↓
official agy CLI
  ↓
stream-json / explicit model / sandbox
```

The adapter parses provider events instead of trusting process exit code alone.
