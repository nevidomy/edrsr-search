#!/usr/bin/env bash
# One encoder worker: walks every section in $SECTIONS on one GPU, retrying failures.
# Used by run-multi.sh and add-workers.sh; expects their environment
# (COURT_SEARCH, BATCH_SIZE, MAX_LENGTH, THREADS, SECTIONS, DATA_ROOT, optional
# MACHINE_SHARD, WAIT_FOR_INPUTS, MAX_RETRIES).
#   worker-loop.sh <gpu-index> <worker-name>
set -uo pipefail
gpu=${1:?gpu index}
name=${2:?worker name}
INPUTS_COMPLETE=${INPUTS_COMPLETE:-$DATA_ROOT/.inputs-complete}
for s in $SECTIONS; do
  while :; do
    final=1
    if [ "${WAIT_FOR_INPUTS:-0}" = "1" ] && [ ! -f "$INPUTS_COMPLETE" ]; then final=0; fi
    for attempt in $(seq 1 "${MAX_RETRIES:-3}"); do
      echo "$(date -Is) $name $s attempt $attempt"
      if CUDA_VISIBLE_DEVICES=$gpu OMP_NUM_THREADS=$THREADS RAYON_NUM_THREADS=$THREADS \
         "$COURT_SEARCH" embed-passages --section "$s" --worker "$name" \
           --batch-size "$BATCH_SIZE" --max-length "$MAX_LENGTH" \
           ${MACHINE_SHARD:+--shard "$MACHINE_SHARD"} >/dev/null; then
        break
      fi
      echo "$(date -Is) $name $s failed; retrying in 30s"
      sleep 30
    done
    [ "$final" = 1 ] && break
    echo "$(date -Is) $name $s: inputs still arriving; rescanning in 60s"
    sleep 60
  done
done
echo "$(date -Is) $name finished"
