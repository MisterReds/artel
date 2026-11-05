#!/usr/bin/env bash
# All engineering gates in one command (docs/SPEC.md §11; CLAUDE.md Engineering rules, rule 9).
#
#   scripts/gates.sh            static gates + tests under coverage + gates on code changed vs master
#   scripts/gates.sh --quick    static gates only (archcheck, ruff, mypy, pip-audit)
#
# Needs: pip install -e ".[dev,gates]". The changed-code gates compare with the merge base of master
# (git fetch origin master first). A gate whose tool is missing says so and is skipped; every other failure
# makes the script exit non-zero after all gates ran.
set -u
cd "$(dirname "$0")/.."
PY="${PYTHON:-python3}"
failed=()

run() {  # run <name> <command...>
  local name="$1"; shift
  printf '\n=== %s ===\n' "$name"
  if "$@"; then :; else failed+=("$name"); fi
}

run archcheck "$PY" scripts/archcheck.py
run ruff "$PY" scripts/lint_ratchet.py ruff
run mypy "$PY" scripts/lint_ratchet.py mypy
if "$PY" -c 'import pip_audit' 2>/dev/null; then
  run pip-audit "$PY" -m pip_audit --strict --progress-spinner off .
else
  echo "pip-audit: SKIPPED - not installed (pip install -e '.[gates]')"
fi

if [ "${1:-}" != "--quick" ]; then
  if "$PY" -c 'import coverage' 2>/dev/null; then
    run "tests under coverage" "$PY" -m coverage run -m pytest -q -p no:cacheprovider
    "$PY" -m coverage json -o coverage.json -q
    run coverage "$PY" scripts/coverage_gate.py check coverage.json
    run "crap (changed functions)" "$PY" scripts/changed_gates.py crap --coverage coverage.json
  else
    echo "coverage, crap: SKIPPED - coverage is not installed (pip install -e '.[gates]')"
  fi
  run "duplicate code (changed files)" "$PY" scripts/changed_gates.py dup
  run "mutation (changed files)" "$PY" scripts/changed_gates.py mutation --budget-min 10
fi

printf '\n'
if [ "${#failed[@]}" -gt 0 ]; then
  echo "FAILED gates: ${failed[*]}"
  exit 1
fi
echo "all gates passed"
