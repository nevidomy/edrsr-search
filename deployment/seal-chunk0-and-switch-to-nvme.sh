#!/usr/bin/env bash
# Stop the F:-direct ingest, snapshot the unfinished first chunk to F:,
# point the checkpoint at chunk 1, then resume ingest on NVMe.
set -euo pipefail
ROOT=/home/nevidomy/work/edrsr
cd "$ROOT"
export COURT_SEARCH_CONFIG="$ROOT/config/local.yaml"
LOG="$ROOT/data/reports/full-v1-ingest.log"
mkdir -p /mnt/f/edrsr/snapshots/full-v1

echo "$(date -Is) stopping ingest so chunk 0 can be copied to F:" | tee -a "$LOG"
pkill -INT -f 'court-search ingest-full' || true
for _ in $(seq 1 60); do
  if ! pgrep -f 'court-search ingest-full' >/dev/null; then
    break
  fi
  sleep 2
done
if pgrep -f 'court-search ingest-full' >/dev/null; then
  echo "ingest still running; sending TERM" | tee -a "$LOG"
  pkill -TERM -f 'court-search ingest-full' || true
  sleep 5
fi
if pgrep -f 'court-search ingest-full' >/dev/null; then
  echo "ingest did not stop" >&2
  exit 1
fi

echo "$(date -Is) snapshotting decisions-full-v1-c000 to F:" | tee -a "$LOG"
"$ROOT/.venv/bin/python" - <<'PY'
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from court_search.indexing.mappings import decision_chunk_index_name, snapshot_id
from court_search.indexing.opensearch import OpenSearchIndex
from court_search.runtime import load_config

cfg = load_config(Path("/home/nevidomy/work/edrsr/config/local.yaml"))
index = OpenSearchIndex(cfg.search)
if not index.ping():
    raise SystemExit("OpenSearch is not reachable")

name = "full-v1"
chunk = 0
repo = cfg.search.snapshot_repo
location = f"{cfg.search.snapshot_container_path}/{name}"
index_name = decision_chunk_index_name(name, chunk)
snap = snapshot_id(chunk)
Path("/mnt/f/edrsr/snapshots/full-v1").mkdir(parents=True, exist_ok=True)
index.ensure_snapshot_repo(repo, location)
index.finalize_index(index_name)
store_bytes = index.index_store_bytes(index_name)
print(f"{index_name} store_bytes={store_bytes} snapshot={snap}", flush=True)
if not index.snapshot_exists(repo, snap):
    info = index.snapshot_index(repo, snap, index_name)
    print(f"snapshot state={info.get('state')}", flush=True)
else:
    print("snapshot already exists", flush=True)

path = Path("/home/nevidomy/work/edrsr/data/normalized/full-v1/checkpoint.json")
checkpoint = json.loads(path.read_text(encoding="utf-8"))
sealed = {
    "id": chunk,
    "index": index_name,
    "snapshot": snap,
    "store_bytes": store_bytes,
    "accepted_after": int(checkpoint.get("accepted") or 0),
    "sealed_at": datetime.now(UTC).isoformat(),
}
chunks = [item for item in (checkpoint.get("chunks") or []) if item.get("id") != chunk]
chunks.append(sealed)
checkpoint["chunks"] = chunks
checkpoint["current_chunk"] = chunk + 1
checkpoint["updated_at"] = datetime.now(UTC).isoformat()
path.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"checkpoint current_chunk={checkpoint['current_chunk']} sealed={len(chunks)}", flush=True)
PY

echo "$(date -Is) chunk 0 is on F:; starting NVMe ingest for later chunks" | tee -a "$LOG"
exec /home/nevidomy/work/edrsr/deployment/run-full.sh
