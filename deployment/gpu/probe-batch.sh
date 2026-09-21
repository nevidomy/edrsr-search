#!/usr/bin/env bash
# Find a batch size that saturates this GPU. Uses one parquet file, then stops.
# Watch nvidia-smi in another terminal. Pick the largest batch that fits and
# keeps utilization high; reuse it for every section.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
SECTION=${1:-civil}
export COURT_SEARCH_CONFIG=${COURT_SEARCH_CONFIG:-$ROOT/config/local.yaml}
cd "$ROOT"

echo "section=$SECTION  (one file only; not a full embed)"
for batch in ${BATCHES:-8 16 32 64 96 128}; do
  echo "=== batch $batch ==="
  rm -rf "$ROOT/data/embeddings/probe-batch/$SECTION"
  if ! "$ROOT/.venv/bin/court-search" embed-passages \
      --section "$SECTION" \
      --batch-size "$batch" \
      --limit-files 1 \
      --spec-name probe-batch; then
    echo "batch $batch failed (likely OOM). use the previous size."
    exit 1
  fi
  if command -v nvidia-smi >/dev/null; then
    nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total --format=csv,noheader
  fi
done
