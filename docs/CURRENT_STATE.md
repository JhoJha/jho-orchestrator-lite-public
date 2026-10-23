# Current State

## Snapshot

- Jho Lite version: **v0.2.5**
- protoAgent pin: **v0.163.0**
- protoAgent commit: `8134031fa1f0f51ec557595189eb44d1eded5da8`
- Primary environment: WSL2 on Windows
- Product workdir at freeze time: `~/repos/client-product`

## Harness status

| Harness | Transport | State | Notes |
|---|---|---|---|
| Codex | ACP | Ready | Exact model and reasoning effort selectable through ACP config |
| OpenCode | ACP | Ready | Exact model and effort selectable through ACP config |
| AGY | Official headless CLI | Ready | Uses stream-json, explicit model/effort mapping, sandbox and workspace confinement |
| NVIDIA NIM | OpenCode access path | Background-only | Not part of the interactive critical path |

## Proven AGY parity

The final read-only smoke at freeze time completed successfully with:

- candidate `agy:gemini-3.8-flash@high`;
- wire model `gemini-3.8-flash-high`;
- provider status `SUCCESS`;
- repository integrity `PASS`;
- no permission block;
- no outside-workspace attempt;
- AGY init cwd equal to the product repo.

## Notifications

WSL → Windows completion notifications are enabled.

Backend policy:

```text
BurntToast already present?
  yes → BurntToast
  no  → built-in Windows Forms NotifyIcon
```

Notification delivery is best-effort and never changes the run verdict.

## Current operating scope

Jho Lite is read-only against the product repository.

Allowed purpose:

- repository exploration;
- planning;
- audits;
- review;
- security review;
- architecture analysis;
- task decomposition;
- evidence collection.

Not yet implemented:

- isolated edit worktrees;
- patch application;
- automated validators after edits;
- reviewer-after-worker workflow;
- automatic routing;
- quota-aware scheduling;
- champion/challenger automation;
- automatic retry/escalation;
- statistical model comparison service;
- distributed execution.

## Freeze decision

Do not expand infrastructure merely because a feature is interesting.

Use the current system on real product tasks and collect evidence.
