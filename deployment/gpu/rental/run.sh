#!/usr/bin/env bash
# Single-section entry point kept for old notes; run-multi.sh does the work
# (one worker per GPU, also fine on a 1-GPU box).
#   SECTION=civil DATA_ROOT=/workspace/edrsr ./deployment/gpu/rental/run.sh
set -euo pipefail
export SECTIONS=${SECTIONS:-${SECTION:-civil}}
exec "$(cd "$(dirname "$0")" && pwd)/run-multi.sh" "$@"
