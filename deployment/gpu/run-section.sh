#!/usr/bin/env bash
# Copy the whole section directory. JSON state travels with the shards:
#   ~/data/edrsr/embeddings/bge-m3-dense-v1/<section>/
# Batch size is remembered in ~/data/edrsr/embeddings/settings.json
# so later sections skip the probe sweep.
#
#   BATCH_SIZE=128 ./deployment/gpu/run-section.sh commercial
#   ./deployment/gpu/run-section.sh civil
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/env.sh"
SECTION=${1:?usage: run-section.sh <section>}
BATCH_SIZE=${BATCH_SIZE:-$(read_setting batch_size 16)}
MAX_LENGTH=${MAX_LENGTH:-$(read_setting max_length 1000)}
LIMIT=${LIMIT_FILES:-}

cd "$ROOT"
echo "batch_size=$BATCH_SIZE max_length=$MAX_LENGTH settings=$SETTINGS" >&2
save_settings "$BATCH_SIZE" "$MAX_LENGTH" >/dev/null
args=(embed-passages --section "$SECTION" --batch-size "$BATCH_SIZE" --max-length "$MAX_LENGTH")
if [ -n "$LIMIT" ]; then
  args+=(--limit-files "$LIMIT")
fi
exec "$COURT_SEARCH" "${args[@]}"
