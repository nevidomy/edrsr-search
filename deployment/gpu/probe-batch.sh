#!/usr/bin/env bash
# Find a batch size that saturates this GPU. Uses one parquet file, then stops.
# Watch nvidia-smi in another terminal. Pick the largest batch that fits and
# keeps utilization high; reuse it for every section.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/env.sh"
SECTION=${1:-civil}
cd "$ROOT"

EMBED_ROOT=$("$PYTHON" -c "from court_search.runtime import load_config; print(load_config().storage.embeddings)")

echo "section=$SECTION  (one file only; not a full embed)"
echo "output=$EMBED_ROOT/probe-batch/$SECTION"
for batch in ${BATCHES:-8 16 32 64 96 128}; do
  echo "=== batch $batch ==="
  rm -rf "$EMBED_ROOT/probe-batch/$SECTION"
  if ! "$COURT_SEARCH" embed-passages \
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
