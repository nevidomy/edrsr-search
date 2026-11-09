#!/usr/bin/env bash
# Copy one section's finished embedding shards to the archive disk and load them into OpenSearch.
# Both steps resume: rsync skips copied files, index-passages skips files listed in
# os-index.json (and re-sent passages overwrite by passage_id, never duplicate).
#
#   ./deployment/gpu/pull-and-index.sh administrative_offense gpu-box:data/edrsr/embeddings/bge-m3-dense-v1/administrative_offense/
#   ./deployment/gpu/pull-and-index.sh civil            # no source: index what is already local
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/env.sh"
SECTION=${1:?usage: pull-and-index.sh <section> [rsync-source/]}
SRC=${2:-}
DEST=${DEST:-$(spec_root)/$SECTION/}
mkdir -p "$DEST"
if [ -n "$SRC" ]; then
  echo "$(date -Is) rsync $SRC -> $DEST"
  rsync -a --partial --inplace --omit-dir-times --exclude '*.tmp' --exclude 'claims/' --exclude 'workers/' "$SRC" "$DEST"
  echo "$(date -Is) copied: $(ls "$DEST/shards" | wc -l) shards, $(du -sh "$DEST/shards" | cut -f1)"
fi
echo "$(date -Is) index-passages --section $SECTION"
nice -n 10 "$COURT_SEARCH" index-passages --section "$SECTION"
echo "$(date -Is) done"
