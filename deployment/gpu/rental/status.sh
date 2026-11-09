#!/usr/bin/env bash
# Progress and ETA for an encoding run (rental box or home). Read-only.
#   ./deployment/gpu/rental/status.sh            # uses COURT_SEARCH_CONFIG / SECTIONS
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
DATA_ROOT=${DATA_ROOT:-/workspace/edrsr}
if [ -z "${COURT_SEARCH_CONFIG:-}" ] && [ -f "$DATA_ROOT/work/rental.yaml" ]; then
  export COURT_SEARCH_CONFIG=$DATA_ROOT/work/rental.yaml
fi
# shellcheck source=../env.sh
source "$ROOT/deployment/gpu/env.sh"
export SECTIONS=${SECTIONS:-${SECTION:-civil}}
"$PYTHON" - <<'PY'
import json, os, time
from datetime import datetime
from court_search.embed.run import _section_dir, _source_for_section, parse_shard, shard_files
from court_search.embed.sections import SECTION_BY_ID
from court_search.runtime import load_config

config = load_config()
shard = parse_shard(os.environ.get("MACHINE_SHARD"))
now = time.time()
total_left = 0.0
pace = (0.0, 0.0)  # (last activity time, bytes/s)
for section in os.environ["SECTIONS"].split():
    source = _source_for_section(config, SECTION_BY_ID[section])
    files = sorted((source.path / "data").glob("*.parquet"))
    if shard:
        files = shard_files(files, *shard)
    out = _section_dir(config, section)
    size = {f.name: f.stat().st_size for f in files}
    done_bytes = passages = done = 0
    first = None
    for f in files:
        state_path = out / "files" / f"{f.stem}.json"
        if not state_path.exists():
            continue
        state = json.loads(state_path.read_text())
        if state.get("status") == "done" and state.get("has_vectors") and (out / state["shard"]).exists():
            done += 1
            done_bytes += size[f.name]
            passages += int(state.get("passages") or 0)
    claims = list((out / "claims").glob("*.claim")) if (out / "claims").is_dir() else []
    if claims:
        first = min(c.stat().st_mtime for c in claims)
    all_bytes = sum(size.values())
    pct = 100 * done_bytes / all_bytes if all_bytes else 0
    line = f"{section:24s} files {done}/{len(files)}  {pct:5.1f}% of bytes  passages {passages:,}"
    workers = []
    if (out / "workers").is_dir():
        for w in sorted((out / "workers").glob("*.json")):
            info = json.loads(w.read_text())
            age = now - datetime.fromisoformat(info["updated_at"]).timestamp()
            workers.append(f"{info['worker']}:{info.get('passages_per_s') or '-'}/s{' STALE' if age > 1800 else ''}")
    if first and done_bytes:
        # Bytes finished during this run vs wall time since the first claim.
        finished = [out / "shards" / f"{c.stem}.parquet" for c in claims if c.stem + ".parquet" in size]
        finished = [path for path in finished if path.exists()]
        run_done = sum(size[path.name] for path in finished)
        last = max((path.stat().st_mtime for path in finished), default=first)
        end = now if done < len(files) else last
        if run_done and end > first:
            rate = run_done / (end - first)
            left = all_bytes - done_bytes
            pace = max(pace, (last, rate))  # the most recently active section sets the pace
            if left:
                line += f"  ETA {left / rate / 3600:5.1f} h"
    total_left += all_bytes - done_bytes
    print(line)
    if workers:
        print("   " + "  ".join(workers))
if total_left and pace[1]:
    print(f"all sections: ~{total_left / pace[1] / 3600:.1f} h left at this run's pace")
PY
