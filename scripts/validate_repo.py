#!/usr/bin/env python3
from __future__ import annotations
from pathlib import Path
import json, re, sys

ROOT = Path(__file__).resolve().parents[1]

required = [
    "bin/jho",
    "runner/jho_lite.py",
    "runner/jho_git.py",
    "runner/jho_safe.py",
    "runner/jho_operator.py",
    "runner/jho_pipeline.py",
    "runner/jho_review.py",
    "runner/jho_tui.py",
    "runner/jho_validate.py",
    "runner/jho_worker.py",
    "runner/jho_worktree.py",
    "tests/test_jho_closure.py",
    "tests/test_jho_hotfix.py",
    "tests/test_jho_operator.py",
    "tests/test_jho_pipeline.py",
    "tests/test_jho_sanitization.py",
    "tests/test_jho_tui.py",
    "config/candidate_registry_v1.json",
    "config/routing_policy_v1.json",
    "schemas/run_event_v1.schema.json",
    "docs/00_START_HERE.md",
    "docs/HANDOFF_CURRENT.md",
    "protoagent.lock",
]

for rel in required:
    p = ROOT / rel
    if not p.is_file():
        print(f"FAIL missing: {rel}", file=sys.stderr)
        sys.exit(2)

# Validate JSON
for rel in [
    "config/candidate_registry_v1.json",
    "config/routing_policy_v1.json",
    "schemas/run_event_v1.schema.json",
]:
    json.loads((ROOT / rel).read_text(encoding="utf-8"))

# Strong secret-ish patterns. Intentionally conservative; false positives should be reviewed.
patterns = [
    re.compile(r"(?i)(api[_-]?key|secret|access[_-]?token|refresh[_-]?token)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{16,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
]

skip_dirs = {".git", "__pycache__"}
for p in ROOT.rglob("*"):
    if not p.is_file():
        continue
    if any(part in skip_dirs for part in p.parts):
        continue
    if p.suffix.lower() in {".zip", ".png", ".jpg", ".jpeg", ".gif", ".pdf"}:
        continue
    try:
        text = p.read_text(encoding="utf-8")
    except Exception:
        continue
    # Tests may contain deliberately fake regression sentinels. Only the explicit
    # AUDIT_SYNTHETIC_ namespace is ignored; arbitrary test credentials still fail.
    scan_text = text
    try:
        if p.is_relative_to(ROOT / "tests"):
            scan_text = re.sub(r"AUDIT_SYNTHETIC_[A-Za-z0-9_]+", "SYNTHETIC", text)
    except (AttributeError, ValueError):
        pass
    for pat in patterns:
        m = pat.search(scan_text)
        if m:
            print(f"FAIL possible secret in {p.relative_to(ROOT)}: {m.group(0)[:80]}", file=sys.stderr)
            sys.exit(3)

print("PASS repository structure")
print("PASS JSON")
print("PASS basic secret scan")
