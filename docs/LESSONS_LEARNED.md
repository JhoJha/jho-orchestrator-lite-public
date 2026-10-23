# Lessons Learned

These are intentionally preserved because they are easy to forget after the code stabilizes.

1. **Do not trust exit code 0 as semantic success.** Headless tools can soft-deny required actions.
2. **Initialize observability before branching by harness.** A provider path that bypasses common lifecycle initialization creates blind spots.
3. **Resolve wire-model identity explicitly.** Logical candidate names and provider CLI slugs can differ.
4. **Permissions need pre-run policy.** An unattended agent cannot depend on interactive approval.
5. **Do not “fix” permission friction by granting the whole home directory.** Constrain the workspace instead.
6. **Provider/model/effort/role are distinct experimental dimensions.**
7. **Do not route GPT through OpenCode when direct Codex capacity already exists.**
8. **NVIDIA belongs in background-long until latency evidence says otherwise.**
9. **No chain-of-thought storage.** Liveness counters and tool events are enough.
10. **Notification failure is UX failure, not task failure.**
11. **A lightweight orchestration layer can become a distraction.** Freeze it when the product needs attention.
12. **Temporary architecture needs a documented exit strategy.** Jho Lite may be replaced, not endlessly expanded.
