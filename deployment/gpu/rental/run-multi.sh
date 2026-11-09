#!/usr/bin/env bash
# Rented GPU box (1–8+ GPUs): download sections from Hugging Face, start one
# encoder worker per GPU, and optionally rsync finished shards home (or pull them
# from home with pull-from-rental.sh). Safe to re-run: work resumes per source file.
#
#   export SECTIONS="civil administrative"   # done in this order; workers roll over
#   export DATA_ROOT=/workspace/edrsr         # needs ~1 TB free for both sections
#   export HF_TOKEN=hf_...                    # read token: avoids anonymous HF rate limits (429)
#   export SYNC_DEST=user@host:/path/to/embeddings/bge-m3-dense-v1/   # optional rsync target
#   ./deployment/gpu/rental/run-multi.sh
#
# Several machines: give each the same SECTIONS and its own MACHINE_SHARD=k/n
# (0-based k). Optional: GPUS="0 1 2" (default all), WORKERS_PER_GPU=2 (overlaps CPU
# tokenizing with GPU work), BATCH_SIZE (skips the probe), SKIP_DOWNLOAD=1 (data already
# under DATA_ROOT), MAX_RETRIES (per worker per section, default 3).
# WAIT_FOR_INPUTS=1: inputs are still arriving (e.g. rsync from home). Workers keep
# re-scanning for new files and finish a section only with a pass that started after
# $DATA_ROOT/.inputs-complete exists (touch it when the copy is done).
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
cd "$ROOT"

export SECTIONS=${SECTIONS:-${SECTION:-civil}}
export SECTION=${SECTIONS%% *}
export DATA_ROOT=${DATA_ROOT:-/workspace/edrsr}
export PYTHONUNBUFFERED=1
# Always our own model cache: providers often preset HF_HOME (Vast: /workspace/.hf_home),
# which would hide a model copied into $DATA_ROOT/models. Override with EMBED_HF_HOME.
export HF_HOME=${EMBED_HF_HOME:-$DATA_ROOT/models}
export SETTINGS=${SETTINGS:-$DATA_ROOT/embeddings/settings.json}
export COURT_SEARCH_CONFIG=${COURT_SEARCH_CONFIG:-$DATA_ROOT/work/rental.yaml}
SPEC=bge-m3-dense-v1
LOGS=$DATA_ROOT/logs
mkdir -p "$DATA_ROOT"/{embeddings,models,work,archive} "$LOGS"

# --- Python + a torch build that really runs on these GPUs -------------------
gpu_ok() {
  "$1" - <<'PY' 2>/dev/null
import torch
assert torch.cuda.is_available()
for i in range(torch.cuda.device_count()):
    x = torch.ones(256, 256, device=f"cuda:{i}", dtype=torch.float16)
    (x @ x).sum().item()  # fails with "no kernel image" when torch is too old for the GPU
PY
}
if command -v python3 >/dev/null && gpu_ok python3; then
  PY=python3
  echo "using provider python $(python3 -c 'import sys, torch; print(sys.executable, torch.__version__)')"
else
  # Blackwell (RTX 50xx, B200) needs CUDA 12.8+ wheels; they also cover H100/A100.
  TORCH_INDEX_URL=${TORCH_INDEX_URL:-https://download.pytorch.org/whl/cu128}
  echo "provider torch missing or too old for this GPU; installing torch from $TORCH_INDEX_URL"
  [ -x "$ROOT/.venv/bin/python" ] || python3 -m venv "$ROOT/.venv"
  PY=$ROOT/.venv/bin/python
  "$PY" -m pip install -q -U pip
  "$PY" -m pip install -q 'torch>=2.7' --index-url "$TORCH_INDEX_URL"
  gpu_ok "$PY" || { echo "torch still cannot run kernels on this GPU (driver too old for CUDA 12.8?)" >&2; exit 1; }
fi
if ! "$PY" -c "import sentence_transformers, huggingface_hub, court_search" 2>/dev/null \
   || ! "$PY" -c "from huggingface_hub import HfApi; HfApi.upload_large_folder" 2>/dev/null; then
  echo "installing court-search + embed deps (keeping torch)"
  "$PY" -m pip install -q -e . 'transformers>=4.44,<5' 'sentence-transformers>=3.1,<6' 'huggingface_hub[hf_xet]>=0.32'
fi
if [ "$PY" != python3 ]; then
  export PATH="$ROOT/.venv/bin:$PATH" VIRTUAL_ENV="$ROOT/.venv"
fi
# shellcheck source=../env.sh
source "$ROOT/deployment/gpu/env.sh"

if [ -z "${GPUS:-}" ]; then
  GPUS=$(seq -s ' ' 0 $(( $(nvidia-smi -L | wc -l) - 1 )))
fi
read -r -a GPU_LIST <<<"$GPUS"
NGPU=${#GPU_LIST[@]}
"$PYTHON" -c "import torch; [print('gpu', i, torch.cuda.get_device_name(i)) for i in range(torch.cuda.device_count())]"
echo "sections=[$SECTIONS] gpus=[$GPUS] machine_shard=${MACHINE_SHARD:-0/1} data_root=$DATA_ROOT"

"$PYTHON" "$ROOT/deployment/gpu/rental/write-config.py"
export COURT_SEARCH_CONFIG

if [ -n "${TS_AUTHKEY:-}" ] && command -v curl >/dev/null; then
  command -v tailscale >/dev/null || curl -fsSL https://tailscale.com/install.sh | sh
  tailscale up --authkey="$TS_AUTHKEY" --hostname="${TS_HOSTNAME:-edrsr-rental}" --ssh=true || true
  tailscale ip -4 || true
fi

if [ "${SKIP_DOWNLOAD:-0}" != "1" ]; then
  DL_PIDS=()
  for s in $SECTIONS; do   # sections download in parallel
    SECTION=$s "$ROOT/deployment/gpu/rental/download.sh" >"$LOGS/download-$s.log" 2>&1 &
    DL_PIDS+=($!)
  done
  for pid in "${DL_PIDS[@]}"; do
    wait "$pid" || { echo "download failed; see $LOGS/download-*.log" >&2; exit 1; }
  done
  for s in $SECTIONS; do tail -1 "$LOGS/download-$s.log"; done
fi

# --- batch size: probe once on the first GPU, reuse everywhere ----------------
if [ -z "${BATCH_SIZE:-}" ] && [ "$(read_setting batch_size 0)" = "0" ]; then
  mem=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -1)
  mem=${mem%.*}
  if [ "${mem:-0}" -ge 70000 ]; then BATCHES=${BATCHES:-"64 128 256 384 512"}
  elif [ "${mem:-0}" -ge 40000 ]; then BATCHES=${BATCHES:-"32 64 96 128 192 256"}
  elif [ "${mem:-0}" -ge 20000 ]; then BATCHES=${BATCHES:-"16 32 64 96 128"}
  else BATCHES=${BATCHES:-"8 16 24 32"}; fi
  echo "probe batches=$BATCHES on gpu ${GPU_LIST[0]} (vram_mib=$mem)"
  CUDA_VISIBLE_DEVICES=${GPU_LIST[0]} BATCHES="$BATCHES" "$ROOT/deployment/gpu/probe-batch.sh" "$SECTION"
fi
BATCH_SIZE=${BATCH_SIZE:-$(read_setting batch_size 16)}
MAX_LENGTH=${MAX_LENGTH:-$(read_setting max_length 1000)}
save_settings "$BATCH_SIZE" "$MAX_LENGTH" >/dev/null

# --- shipping finished shards -------------------------------------------------
BG_PIDS=()
cleanup() { for pid in "${BG_PIDS[@]}"; do kill "$pid" 2>/dev/null || true; done; }
trap cleanup EXIT
if [ -n "${SYNC_DEST:-}" ]; then
  SYNC_SRC=$DATA_ROOT/embeddings/$SPEC/ SYNC_DEST=$SYNC_DEST \
    "$ROOT/deployment/gpu/rental/sync-loop.sh" >>"$LOGS/sync.log" 2>&1 &
  BG_PIDS+=($!)
  echo "rsync loop -> $SYNC_DEST (log $LOGS/sync.log)"
fi

# --- one worker per GPU; each walks all sections so no GPU idles at a boundary --
running=$(pgrep -f "embed-passages .*--worker gpu" || true)
if [ -n "$running" ]; then
  echo "workers already running (pids $running); stop them first" >&2
  exit 1
fi
for s in $SECTIONS; do rm -rf "$DATA_ROOT/embeddings/$SPEC/$s/claims"; done

PER_GPU=${WORKERS_PER_GPU:-1}
NWORKERS=$(( NGPU * PER_GPU ))
THREADS=$(( $(nproc) / NWORKERS )); [ "$THREADS" -ge 1 ] || THREADS=1
export COURT_SEARCH BATCH_SIZE MAX_LENGTH THREADS SECTIONS DATA_ROOT
[ -n "${MACHINE_SHARD:-}" ] && export MACHINE_SHARD
[ -n "${WAIT_FOR_INPUTS:-}" ] && export WAIT_FOR_INPUTS
WORKER_PIDS=()
slot=0
for gpu in "${GPU_LIST[@]}"; do
  for k in $(seq 0 $(( PER_GPU - 1 ))); do
    name="gpu${gpu}w${k}-$slot"   # unique even if a GPU is listed twice
    "$ROOT/deployment/gpu/rental/worker-loop.sh" "$gpu" "$name" >>"$LOGS/$name.log" 2>&1 &
    WORKER_PIDS+=($!)
    slot=$(( slot + 1 ))
  done
done
echo "$(date -Is) started $NWORKERS workers on $NGPU GPUs (batch=$BATCH_SIZE threads/worker=$THREADS); logs $LOGS/gpu*.log"
echo "progress: $ROOT/deployment/gpu/rental/status.sh"
wait "${WORKER_PIDS[@]}"

# --- final consistent checkpoint + last shipment ------------------------------
"$PYTHON" - <<'PY'
import json, os
from court_search.embed.run import parse_shard, rebuild_checkpoint
from court_search.runtime import load_config
config = load_config()
for section in os.environ["SECTIONS"].split():
    report = rebuild_checkpoint(config, section_id=section, shard=parse_shard(os.environ.get("MACHINE_SHARD")))
    print(json.dumps(report, ensure_ascii=False))
PY
"$ROOT/deployment/gpu/rental/status.sh" || true
cleanup
BG_PIDS=()
if [ -n "${SYNC_DEST:-}" ]; then
  rsync -a --partial --inplace --omit-dir-times --exclude '*.tmp' --exclude 'claims/' --exclude 'workers/' \
    --exclude 'probe-batch/' "$DATA_ROOT/embeddings/$SPEC/" "$SYNC_DEST"
fi
echo "$(date -Is) rental done: $SECTIONS"
