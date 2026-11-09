#!/usr/bin/env bash
# Add encoder workers to a run that is already going (e.g. a second worker per GPU, so one
# worker chunks text on the CPU while the other keeps the GPU busy). New workers share the
# claims dir with the running ones, so no file is encoded twice and nothing restarts.
#
#   ./deployment/gpu/rental/add-workers.sh              # +1 worker on every GPU
#   EXTRA_PER_GPU=2 GPUS="0 1" ./deployment/gpu/rental/add-workers.sh
#
# Same environment as run-multi.sh (SECTIONS, DATA_ROOT, BATCH_SIZE, WAIT_FOR_INPUTS ...).
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
export SECTIONS=${SECTIONS:-${SECTION:-civil}}
export DATA_ROOT=${DATA_ROOT:-/workspace/edrsr}
export PYTHONUNBUFFERED=1
export HF_HOME=${EMBED_HF_HOME:-$DATA_ROOT/models}
export SETTINGS=${SETTINGS:-$DATA_ROOT/embeddings/settings.json}
export COURT_SEARCH_CONFIG=${COURT_SEARCH_CONFIG:-$DATA_ROOT/work/rental.yaml}
LOGS=$DATA_ROOT/logs
# shellcheck source=../env.sh
source "$ROOT/deployment/gpu/env.sh"
export COURT_SEARCH PYTHON
export BATCH_SIZE=${BATCH_SIZE:-$(read_setting batch_size 16)}
export MAX_LENGTH=${MAX_LENGTH:-$(read_setting max_length 1000)}

if [ -z "${GPUS:-}" ]; then
  GPUS=$(seq -s ' ' 0 $(( $(nvidia-smi -L | wc -l) - 1 )))
fi
read -r -a GPU_LIST <<<"$GPUS"
EXTRA=${EXTRA_PER_GPU:-1}
running=$(pgrep -fc "[e]mbed-passages .*--worker" || true)
total=$(( running + ${#GPU_LIST[@]} * EXTRA ))
export THREADS=$(( $(nproc) / (total > 0 ? total : 1) )); [ "$THREADS" -ge 1 ] || THREADS=1
tag=${TAG:-x$(date +%H%M)}
PIDS=()
for gpu in "${GPU_LIST[@]}"; do
  for k in $(seq 1 "$EXTRA"); do
    name="gpu${gpu}${tag}-$k"
    "$ROOT/deployment/gpu/rental/worker-loop.sh" "$gpu" "$name" >>"$LOGS/$name.log" 2>&1 &
    PIDS+=($!)
  done
done
echo "$(date -Is) added ${#PIDS[@]} workers (now ~$total, threads/worker=$THREADS, batch=$BATCH_SIZE); logs $LOGS/gpu*${tag}*.log"
wait "${PIDS[@]}"
"$PYTHON" - <<'PY'
import os
from court_search.embed.run import parse_shard, rebuild_checkpoint
from court_search.runtime import load_config
config = load_config()
for section in os.environ["SECTIONS"].split():
    print(rebuild_checkpoint(config, section_id=section, shard=parse_shard(os.environ.get("MACHINE_SHARD"))))
PY
echo "$(date -Is) added workers finished"
