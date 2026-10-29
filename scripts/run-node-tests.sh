#!/usr/bin/env bash
set -euo pipefail
exec node --test classify-claude-review/*.test.mjs review/*.test.mjs verify-claude-review/*.test.mjs
