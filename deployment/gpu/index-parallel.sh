#!/usr/bin/env bash
# Start N parallel loaders for one section (user services index-<section>-<k>of<N>).
# Each loads a size-balanced share of the section's shards and resumes on restart.
#   ./deployment/gpu/index-parallel.sh civil 3
#   FROM_DIR=~/data/edrsr/hf/data/civil ./deployment/gpu/index-parallel.sh civil 4   # HF download
#   systemctl --user list-units 'index-civil-*'        # status
#   systemctl --user stop 'index-civil-*'              # stop (safe; resumes later)
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
SECTION=${1:?usage: index-parallel.sh <section> <loaders>}
N=${2:?usage: index-parallel.sh <section> <loaders>}
CONFIG=${COURT_SEARCH_CONFIG:-$ROOT/config/local.yaml}
if systemctl --user list-units --no-legend --state=active "index-$SECTION-*" | grep -q .; then
  echo "loaders for $SECTION are running; stop them first: systemctl --user stop 'index-$SECTION-*'" >&2
  exit 1
fi
# Fold earlier loaders' progress into os-index.json so a new split never re-sends finished files.
COURT_SEARCH_CONFIG="$CONFIG" "$ROOT/.venv/bin/python" -c \
  "from court_search.embed.index_os import merge_loader_reports as m; from court_search.runtime import load_config; import os; d=os.environ.get('FROM_DIR'); print(m(load_config(), section_id='$SECTION', from_dir=d or None))"
for k in $(seq 0 $(( N - 1 ))); do
  unit="index-$SECTION-${k}of$N"
  systemctl --user reset-failed "$unit" 2>/dev/null || true
  systemd-run --user --unit="$unit" --description="index $SECTION shard $k/$N" \
    --setenv=COURT_SEARCH_CONFIG="$CONFIG" --setenv=PYTHONUNBUFFERED=1 \
    --working-directory="$ROOT" -p Nice=10 \
    -p StandardOutput=append:"$ROOT/data/reports/$unit.log" -p StandardError=append:"$ROOT/data/reports/$unit.log" \
    "$ROOT/.venv/bin/court-search" index-passages --section "$SECTION" --shard "$k/$N" ${FROM_DIR:+--from-dir "$FROM_DIR"}
done
