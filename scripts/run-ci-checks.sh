#!/usr/bin/env bash
set -uo pipefail
status=0
bash scripts/run-node-tests.sh || status=1
node scripts/run-python.mjs -B -m unittest discover -s .github/actions/ci-run-context -p 'test_*.py' || status=1
node scripts/run-python.mjs -B scripts/test_run_context_schema.py || status=1
node scripts/run-python.mjs -B -m unittest discover -s .github/actions/post-merge-cadence -p 'test_*.py' || status=1
exit "$status"
