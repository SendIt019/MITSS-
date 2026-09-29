#!/usr/bin/env bash
# Run the matrix runner with the same environment the web app gets.
#
#   scripts/run_matrix.sh check
#   scripts/run_matrix.sh plan backend/matrices/baseline.json
#
# Loads backend/.env the way dev.sh does, so the runner and the web app use
# the same timeouts and data root. run_matrix.py itself never opens .env.
#
# The model load happens inside the one-token preflight probe, and a 14 GB
# model can take longer than the 90 s default, so the probe gets
# MITSS_MATRIX_PREFLIGHT_TIMEOUT seconds, or 300 when that is unset.
#
# Runs from the caller's directory, so a relative matrix path means what it
# says.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"

if [ -f "$REPO/backend/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$REPO/backend/.env"
  set +a
fi

export MITSS_LLM_PREFLIGHT_TIMEOUT="${MITSS_MATRIX_PREFLIGHT_TIMEOUT:-300}"

exec python3 "$REPO/backend/run_matrix.py" "$@"
