#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROTO="${PROTOAGENT_ROOT:-$HOME/tools/jho-orchestrator-lite}"
LOCAL="$PROTO/local_jho"
BIN="$HOME/.local/bin/jho-lite"

EXPECTED_HEAD="8134031fa1f0f51ec557595189eb44d1eded5da8"

echo "=== JHO ORCHESTRATOR LITE BOOTSTRAP ==="
python3 "$ROOT/scripts/validate_repo.py"

test -d "$PROTO/.git" || {
  echo "FAIL protoAgent checkout missing at $PROTO"
  echo "This bootstrap intentionally does not clone/change upstream automatically."
  exit 1
}

ACTUAL_HEAD="$(git -C "$PROTO" rev-parse HEAD)"
test "$ACTUAL_HEAD" = "$EXPECTED_HEAD" || {
  echo "FAIL protoAgent pin mismatch"
  echo "expected=$EXPECTED_HEAD"
  echo "actual=$ACTUAL_HEAD"
  exit 1
}

mkdir -p \
  "$LOCAL/runner" \
  "$LOCAL/config" \
  "$LOCAL/tasks" \
  "$LOCAL/schemas" \
  "$LOCAL/telemetry" \
  "$HOME/.local/bin"

cp "$ROOT/runner/jho_lite.py" "$LOCAL/runner/jho_lite.py"
cp "$ROOT/config/"*.json "$LOCAL/config/"
cp "$ROOT/tasks/"*.md "$LOCAL/tasks/"
cp "$ROOT/schemas/"*.json "$LOCAL/schemas/"
chmod +x "$LOCAL/runner/jho_lite.py"
touch "$LOCAL/telemetry/runs.jsonl"

INFO_EXCLUDE="$PROTO/.git/info/exclude"
touch "$INFO_EXCLUDE"
grep -qxF '/local_jho/' "$INFO_EXCLUDE" || echo '/local_jho/' >> "$INFO_EXCLUDE"

cat > "$BIN" <<EOF
#!/usr/bin/env bash
set -euo pipefail
PROTO="\${PROTOAGENT_ROOT:-$PROTO}"
cd "\$PROTO"
exec uv run python "\$PROTO/local_jho/runner/jho_lite.py" "\$@"
EOF
chmod +x "$BIN"

grep -qxF 'export PATH="$HOME/.local/bin:$PATH"' "$HOME/.bashrc" 2>/dev/null || \
  echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
export PATH="$HOME/.local/bin:$PATH"

echo
echo "=== OPTIONAL AGY LOCAL POLICY ==="
if command -v agy >/dev/null 2>&1; then
  python3 "$ROOT/scripts/configure_agy_local.py"
else
  echo "WARN agy not found; skipped AGY settings"
fi

echo
echo "=== CHECK ==="
jho-lite check

echo
echo "=== BOOTSTRAP COMPLETE ==="
echo "Runtime overlay: $LOCAL"
echo "Launcher: $BIN"
