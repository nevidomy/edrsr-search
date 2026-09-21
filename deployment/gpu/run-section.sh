#!/usr/bin/env bash
# Copy the whole section directory. JSON state travels with the shards:
#   spec.json  checkpoint.json  report.json  files/*.json  shards/*.parquet
# Resume is per source parquet file; re-running skips completed files.
#
#   ./deployment/gpu/run-section.sh civil
#   BATCH_SIZE=64 ./deployment/gpu/run-section.sh administrative
#
# 3060 12GB: start BATCH_SIZE=16. 5090 32GB: probe, then 64–128.
# Sections: civil administrative criminal commercial administrative_offense
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
SECTION=${1:?usage: run-section.sh <section>}
export COURT_SEARCH_CONFIG=${COURT_SEARCH_CONFIG:-$ROOT/config/local.yaml}
BATCH_SIZE=${BATCH_SIZE:-16}
MAX_LENGTH=${MAX_LENGTH:-1000}
LIMIT=${LIMIT_FILES:-}

cd "$ROOT"
args=(embed-passages --section "$SECTION" --batch-size "$BATCH_SIZE" --max-length "$MAX_LENGTH")
if [ -n "$LIMIT" ]; then
  args+=(--limit-files "$LIMIT")
fi
exec "$ROOT/.venv/bin/court-search" "${args[@]}"
