#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

python3 scripts/validate_repo.py
PYTHONDONTWRITEBYTECODE=1 python3 -c "from pathlib import Path; [compile(p.read_text(encoding='utf-8'), str(p), 'exec') for p in Path('runner').glob('*.py')]"
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_*.py'

if [ ! -d .git ]; then
  git init -b main
fi

git add \
  .gitignore AGENTS.md README.md CHANGELOG.md SECURITY.md MANIFEST.txt protoagent.lock \
  bin runner tests config tasks schemas scripts docs .github

echo
echo "=== STAGED FILES ==="
git status --short

echo
echo "Review the staged files before committing."
echo "Suggested first commit:"
echo '  git commit -m "chore: preserve Jho Orchestrator Lite v0.2.5"'
