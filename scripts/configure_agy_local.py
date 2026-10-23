#!/usr/bin/env python3
from __future__ import annotations
import json, shutil, sys
from datetime import datetime
from pathlib import Path

settings = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"
product = (Path.home() / "repos" / "client-product").resolve()

if settings.exists():
    try:
        data = json.loads(settings.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"FAIL cannot parse {settings}: {exc}", file=sys.stderr)
        sys.exit(2)
    backup = settings.with_name(
        "settings.json.bak-jho-bootstrap-" + datetime.now().strftime("%Y%m%dT%H%M%S")
    )
    shutil.copy2(settings, backup)
    print(f"Backup: {backup}")
else:
    settings.parent.mkdir(parents=True, exist_ok=True)
    data = {}

if not isinstance(data, dict):
    print("FAIL settings root is not an object", file=sys.stderr)
    sys.exit(2)

data["enableTerminalSandbox"] = True
data["toolPermission"] = "proceed-in-sandbox"

perms = data.setdefault("permissions", {})
if not isinstance(perms, dict):
    print("FAIL permissions is not an object", file=sys.stderr)
    sys.exit(2)

allow = list(perms.get("allow") or [])
deny = list(perms.get("deny") or [])
ask = list(perms.get("ask") or [])

target = f"read_file({product})"
if any(x in {"read_file(*)", target} for x in deny):
    print("FAIL existing deny rule outranks required repo read access", file=sys.stderr)
    sys.exit(3)
if any(x in {"read_file(*)", target, "command(*)"} for x in ask):
    print("FAIL existing ask rule would break unattended headless execution", file=sys.stderr)
    sys.exit(3)

if target not in allow:
    allow.append(target)

for rule in [
    "command(sudo)",
    "command(su)",
    "command(rm -rf)",
    "command(git push)",
    "command(git merge)",
    "command(git commit)",
    "command(git reset --hard)",
    "command(git clean -f)",
    "command(git clean -fd)",
    "unsandboxed(*)",
    "write_file(.git/)",
    "read_file(~/.ssh/)",
    "write_file(~/.ssh/)",
    "read_file(~/.aws/)",
    "write_file(~/.aws/)",
    "read_file(~/.gnupg/)",
    "write_file(~/.gnupg/)",
]:
    if rule not in deny:
        deny.append(rule)

perms["allow"] = allow
perms["deny"] = deny
perms["ask"] = ask

tmp = settings.with_suffix(".json.tmp")
tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
tmp.replace(settings)
print(f"Configured AGY settings: {settings}")
print(f"Scoped read: {target}")
