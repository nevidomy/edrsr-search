#!/usr/bin/env bash
# Pull one EDRSR section from Hugging Face onto the rental box.
# Prefer HF from a datacenter IP. Home upload of 37G tspk is usually slower.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
# shellcheck source=../env.sh
source "$ROOT/deployment/gpu/env.sh"
SECTION=${SECTION:-civil}
DATA_ROOT=${DATA_ROOT:-/workspace/edrsr}

folder=$("$PYTHON" -c "from court_search.embed.sections import SECTION_BY_ID; print(SECTION_BY_ID['$SECTION']['folder'])")
repo=$("$PYTHON" -c "from court_search.embed.sections import SECTION_BY_ID; print(SECTION_BY_ID['$SECTION']['repo'])")
dest=$DATA_ROOT/$folder
mkdir -p "$dest"

# Always run: snapshot_download is incremental (finished files are skipped), and a
# count-based "already have it" shortcut would accept a half-finished download.

if ! "$PYTHON" -c "import huggingface_hub" 2>/dev/null; then
  "$PYTHON" -m pip install -q 'huggingface_hub>=0.24'
fi

echo "HF snapshot_download $repo -> $dest"
if [ -n "${HF_TOKEN:-${HUGGING_FACE_HUB_TOKEN:-}}" ]; then
  export HF_TOKEN="${HF_TOKEN:-$HUGGING_FACE_HUB_TOKEN}"
fi
HF_REPO=$repo HF_DEST=$dest "$PYTHON" - <<'PY'
import os
from huggingface_hub import snapshot_download
snapshot_download(
    repo_id=os.environ["HF_REPO"],
    repo_type="dataset",
    local_dir=os.environ["HF_DEST"],
    # Many small files: per-file latency, not bandwidth, is the limit at the default of 8.
    max_workers=int(os.environ.get("HF_MAX_WORKERS", "32")),
)
PY
echo "download done: $(find "$dest/data" -name '*.parquet' | wc -l) parquet files"
