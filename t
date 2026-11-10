#!/bin/sh
# Run this repo's tests with the pinned pytest, from the repo root wherever it's called,
# so name test files from the root. Any argument runs in one process (-n0).
#
#   heavy --quick -- ./t test_wt_ship.py -k push     some tests, in one process (-n0)
#   bgrun --wait -o LOG -- heavy -- ./t              the whole suite, in parallel
#
# It runs no heavy itself, so heavy-gate still sees the heavy in the command and asks
# for its timeout or background. The pins are requirements-dev.txt's, as the hooks use.
set -e
cd "$(dirname "$0")"
if [ "$#" -gt 0 ]; then
  set -- -n0 "$@"
fi
exec uv run -q --no-project --with-requirements requirements-dev.txt \
  pytest -q -p no:cacheprovider "$@"
