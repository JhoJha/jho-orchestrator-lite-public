#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "=== JHO ORCHESTRATOR LITE DOCTOR ==="
python3 "$ROOT/scripts/validate_repo.py"
PYTHONDONTWRITEBYTECODE=1 python3 -c "from pathlib import Path; [compile(p.read_text(encoding='utf-8'), str(p), 'exec') for p in Path('$ROOT/runner').glob('*.py')]"

echo
echo "protoAgent pin:"
cat "$ROOT/protoagent.lock"

PROTO="${PROTOAGENT_ROOT:-$HOME/tools/jho-orchestrator-lite}"
if [ -d "$PROTO/.git" ]; then
  echo
  echo "Installed protoAgent:"
  echo "head=$(git -C "$PROTO" rev-parse HEAD)"
  git -C "$PROTO" describe --tags --always --dirty || true
else
  echo
  echo "WARN protoAgent checkout not found at $PROTO"
fi

if command -v jho-lite >/dev/null 2>&1; then
  echo
  echo "Installed Jho Lite:"
  PYTHONDONTWRITEBYTECODE=1 jho-lite check || true
else
  echo
  echo "WARN jho-lite command not installed"
fi
