#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-$ROOT_DIR/.venv/bin/python}"

# CHG-057 D-09 / architecture baseline §5.13: the suite must use throwaway
# directories, never the checkout's .local/. Only *new* paths are compared:
# the developer's own dev Agent may be running and writing its database there,
# and that is not the tests' doing. A test that resolves into the checkout
# creates a path, which is what this catches -- including the case that matters
# most, a clean clone where .local/ does not exist yet.
snapshot_local() {
    if [ -d .local ]; then
        find .local -mindepth 1 -print | sort
    fi
}

BEFORE="$(snapshot_local)"

set +e
PYTHONPATH="${PYTHONPATH:-src}" "$PYTHON_BIN" -m unittest discover -s tests
STATUS=$?
set -e

AFTER="$(snapshot_local)"
ADDED="$(comm -13 <(printf '%s\n' "$BEFORE") <(printf '%s\n' "$AFTER") | sed '/^[[:space:]]*$/d' || true)"

if [ -n "$ADDED" ]; then
    echo "" >&2
    echo "ERROR: the test suite created paths under the checkout's .local/:" >&2
    printf '  %s\n' "$ADDED" >&2
    echo "Tests must use tmpdir/data, tmpdir/logs and tmpdir/runtime (CHG-057 D-09)." >&2
    exit 1
fi

exit "$STATUS"
