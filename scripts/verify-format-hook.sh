#!/usr/bin/env bash
#
# Verify the PostToolUse ruff check --fix + black hook in .claude/settings.json.
#
# It extracts the REAL command from settings.json and feeds it the same
# stdin JSON Claude Code would, then asserts the side effects:
#   1. a .py file with style + lint violations comes out format-clean and lint-clean
#   2. a non-.py file is left untouched (the *.py guard works)
#
# Re-runnable. Exits non-zero on any failure. Run from anywhere:
#   bash scripts/verify-format-hook.sh
#
set -euo pipefail

# Resolve repo root from this script's location so it works from any cwd.
cd "$(dirname "$0")/.."

SETTINGS=".claude/settings.json"
[ -f "$SETTINGS" ] || { echo "FAIL: $SETTINGS not found"; exit 1; }

# Pull the live command out of settings.json: we test what actually runs.
CMD=$(jq -r '.hooks.PostToolUse[] | select(.matcher=="Write|Edit") | .hooks[0].command' "$SETTINGS")
[ -n "$CMD" ] && [ "$CMD" != "null" ] || { echo "FAIL: no Write|Edit PostToolUse hook in $SETTINGS"; exit 1; }
echo "Testing hook command:"
echo "  $CMD"
echo

probe="/tmp/verify-format-hook.$$.py"
nonpy="/tmp/verify-format-hook.$$.md"
trap 'rm -f "$probe" "$nonpy"' EXIT

# Simulate the hook firing on a given file path.
fire() { printf '{"tool_name":"Edit","tool_input":{"file_path":"%s"}}' "$1" | eval "$CMD"; }

# --- Case 1: Python file with violations should be auto-fixed --------------
# Bad spacing (black) + unused import (ruff F401).
printf 'x={ "a":1 ,"b":2}\nimport os\n' > "$probe"
fire "$probe"
if ! uv run black --check "$probe" >/dev/null 2>&1; then
  echo "FAIL: .py file is not black-clean after hook ran:"; cat "$probe"; exit 1
fi
if ! uv run ruff check "$probe" >/dev/null 2>&1; then
  echo "FAIL: ruff still reports issues after hook ran:"; cat "$probe"; exit 1
fi
echo "PASS: .py file was lint-fixed (ruff check) and formatted (black)"

# --- Case 2: non-Python file must be left untouched ------------------------
printf '#  Title\n\n\n' > "$nonpy"
before="$(cat "$nonpy")"
fire "$nonpy"
if [ "$(cat "$nonpy")" != "$before" ]; then
  echo "FAIL: non-.py file was modified: the *.py guard is broken"; exit 1
fi
echo "PASS: non-.py file left untouched"

echo
echo "ALL CHECKS PASSED"