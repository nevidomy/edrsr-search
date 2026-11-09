#!/usr/bin/env bash
# Download sections of the published dataset (default nevidomy/edrsr-search) so they can be
# indexed without any GPU work. Resumable; finished files are skipped.
#
#   SECTIONS="commercial criminal" ./deployment/gpu/rental/pull-from-hf.sh
#   court-search index-passages --section commercial --from-dir "$LOCAL_ROOT/data/commercial"
#
# Files land in $LOCAL_ROOT/data/<section>/*.parquet (default ~/data/edrsr/hf).
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
# shellcheck source=../env.sh
source "$ROOT/deployment/gpu/env.sh"
export HF_REPO=${HF_REPO:-nevidomy/edrsr-search}
export SECTIONS=${SECTIONS:-sample}
export LOCAL_ROOT=${LOCAL_ROOT:-$HOME/data/edrsr/hf}
"$PYTHON" - <<'PY'
import os
from huggingface_hub import snapshot_download

patterns = [f"data/{section}/*" for section in os.environ["SECTIONS"].split()] + ["README.md"]
path = snapshot_download(repo_id=os.environ["HF_REPO"], repo_type="dataset", local_dir=os.environ["LOCAL_ROOT"],
                         allow_patterns=patterns, max_workers=int(os.environ.get("HF_MAX_WORKERS", "8")))
print(f"downloaded {patterns} into {path}")
for section in os.environ["SECTIONS"].split():
    print(f"index with: court-search index-passages --section {section} --from-dir {path}/data/{section}")
PY
