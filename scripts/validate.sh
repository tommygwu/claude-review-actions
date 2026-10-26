#!/usr/bin/env bash
set -euo pipefail
exec "${AGENTS_REPO:-$HOME/.agents}/skills/governed-gates/scripts/execute-plan.sh" --plan .repository-suite-plan.json --scope "$@"
