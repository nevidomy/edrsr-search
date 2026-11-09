#!/usr/bin/env bash
# Incremental rsync of finished shards. Safe to re-run. Skips *.tmp.
# SYNC_DEST examples:
#   user@100.x.y.z:/path/to/embeddings/bge-m3-dense-v1/civil/     (tailnet IP)
#   user@home-pc:/path/to/embeddings/bge-m3-dense-v1/civil/
set -euo pipefail
SECTION=${SECTION:-civil}
DATA_ROOT=${DATA_ROOT:-/workspace/edrsr}
SRC=${SYNC_SRC:-$DATA_ROOT/embeddings/bge-m3-dense-v1/$SECTION/}
DEST=${SYNC_DEST:?set SYNC_DEST to user@host:/path/to/embeddings/bge-m3-dense-v1/$SECTION/}
INTERVAL=${SYNC_INTERVAL_S:-300}

if [ ! -d "$SRC" ]; then
  echo "waiting for $SRC"
  while [ ! -d "$SRC" ]; do
    sleep 15
  done
fi

echo "sync $SRC -> $DEST every ${INTERVAL}s"
while true; do
  rsync -a --partial --inplace --omit-dir-times \
    --exclude '*.tmp' \
    --exclude 'probe-batch/' \
    --exclude 'claims/' \
    --exclude 'workers/' \
    "$SRC" "$DEST" || echo "rsync failed; will retry"
  sleep "$INTERVAL"
done
