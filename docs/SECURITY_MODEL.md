# Security Model

## Principle

Autonomous does not mean unrestricted.

```text
autonomy
+ isolation
+ predeclared permissions
+ deterministic checks
+ bounded scope
```

## Current mode

Read-only main product checkout.

The runner compares repository state before and after execution.

## AGY

AGY uses:

- official headless CLI;
- sandbox;
- explicit workspace;
- scoped read permission;
- denial of dangerous operations;
- no `--dangerously-skip-permissions` as routine policy.

A soft-denied required tool must not be counted as success merely because the CLI exits zero.

## Future edit mode

Required shape:

```text
main checkout
    │
    └── disposable isolated worktree
            │
            ├── worker edits
            ├── deterministic validators
            ├── reviewer
            └── human/explicit merge decision
```

Initial policy should continue to deny:

- push;
- merge;
- deploy;
- sudo;
- destructive filesystem operations;
- writes outside worktree;
- credential access.

## Secrets

Never commit:

- API keys;
- OAuth tokens;
- provider credentials;
- AGY settings containing sensitive data;
- telemetry with sensitive prompts/data;
- run artifacts;
- `.env` files;
- SSH/cloud credentials.
