#!/usr/bin/env bash
# Run at home: pull finished shards from a rented box over SSH. Nothing has to
# reach into the home network. Incremental and safe to re-run.
#
#   ./deployment/gpu/rental/pull-from-rental.sh root@<box-ip> [--loop]
#   SSH_PORT=41234 ./deployment/gpu/rental/pull-from-rental.sh root@<box-ip> --loop
#
# Pulls the whole spec dir (all sections) by default. Optional: SECTIONS="civil",
# REMOTE_ROOT (default /workspace/edrsr/embeddings/bge-m3-dense-v1),
# LOCAL_ROOT (default: the config's archive or embeddings dir), PULL_INTERVAL_S (600).
set -euo pipefail
HOST=${1:?usage: pull-from-rental.sh user@host [--loop]}
MODE=${2:-}
REMOTE_ROOT=${REMOTE_ROOT:-${RENTAL_PATH:-/workspace/edrsr/embeddings/bge-m3-dense-v1}}
ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
# shellcheck source=../env.sh
source "$ROOT/deployment/gpu/env.sh"
LOCAL_ROOT=${LOCAL_ROOT:-$(spec_root)}
INTERVAL=${PULL_INTERVAL_S:-600}
SSH_CMD="ssh -o BatchMode=yes -o ServerAliveInterval=30 -o StrictHostKeyChecking=accept-new${SSH_PORT:+ -p $SSH_PORT}"

pull_once() {
  local src dest
  if [ -n "${SECTIONS:-}" ]; then
    for s in $SECTIONS; do
      mkdir -p "$LOCAL_ROOT/$s"
      rsync -a --partial --inplace --omit-dir-times -e "$SSH_CMD" \
        --exclude '*.tmp' --exclude 'claims/' --exclude 'workers/' --exclude 'probe-batch/' \
        "$HOST:${REMOTE_ROOT%/}/$s/" "$LOCAL_ROOT/$s/"
    done
  else
    mkdir -p "$LOCAL_ROOT"
    rsync -a --partial --inplace --omit-dir-times -e "$SSH_CMD" \
      --exclude '*.tmp' --exclude 'claims/' --exclude 'workers/' --exclude 'probe-batch/' \
      "$HOST:${REMOTE_ROOT%/}/" "$LOCAL_ROOT/"
  fi
  echo "$(date -Is) pulled $HOST:$REMOTE_ROOT -> $LOCAL_ROOT ($(du -sh "$LOCAL_ROOT" | cut -f1))"
}

if [ "$MODE" = "--loop" ]; then
  echo "pull every ${INTERVAL}s; Ctrl-C to stop"
  while true; do
    pull_once || echo "$(date -Is) rsync failed; will retry"
    sleep "$INTERVAL"
  done
else
  pull_once
fi
