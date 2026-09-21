#!/usr/bin/env bash
# Optional one-time sweep. Successful sizes are written to
# ~/data/edrsr/embeddings/settings.json so run-section.sh reuses them.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/env.sh"
SECTION=${1:-civil}
cd "$ROOT"

EMBED_ROOT=$("$PYTHON" -c "from court_search.runtime import load_config; print(load_config().storage.embeddings)")
MAX_LENGTH=${MAX_LENGTH:-1000}
ok=

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
    echo "batch $batch failed (likely OOM). keeping $ok"
    break
  fi
  ok=$batch
  save_settings "$ok" "$MAX_LENGTH"
  if command -v nvidia-smi >/dev/null; then
    nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total --format=csv,noheader
  fi
done
if [ -n "$ok" ]; then
  echo "saved batch_size=$ok to $SETTINGS"
else
  echo "no successful batch" >&2
  exit 1
fi
