#!/usr/bin/env bash
# Index all five jurisdictions on the NVMe WSL disk in ~80GiB chunks,
# snapshot each chunk to /mnt/f/edrsr/snapshots, then delete the local
# chunk. pilot-v1 stays active. Restore onto F: only after ingest finishes.
# Progress: data/reports/full-v1-status.json and /mnt/f/edrsr/full-v1-status.json
set -euo pipefail
cd /home/nevidomy/work/edrsr
export COURT_SEARCH_CONFIG=/home/nevidomy/work/edrsr/config/local.yaml
mkdir -p /home/nevidomy/work/edrsr/data/reports
mkdir -p /mnt/f/edrsr/snapshots /mnt/f/edrsr/opensearch
chmod a+rwx /mnt/f/edrsr /mnt/f/edrsr/snapshots /mnt/f/edrsr/opensearch || true
LOG=/home/nevidomy/work/edrsr/data/reports/full-v1-ingest.log
COMPOSE=/home/nevidomy/work/edrsr/deployment/docker-compose.yml
ARCHIVE=/home/nevidomy/work/edrsr/deployment/docker-compose.archive.yml

docker_cmd() {
  if command -v sg >/dev/null && getent group docker >/dev/null; then
    sg docker -c "$*"
  else
    bash -lc "$*"
  fi
}

echo "$(date -Is) OpenSearch on NVMe, snapshots on F:" | tee -a "$LOG"
docker_cmd "docker compose -f $ARCHIVE down --remove-orphans" || true
docker_cmd "docker compose -f $COMPOSE up -d --force-recreate opensearch"

for _ in $(seq 1 90); do
  if curl -sf http://127.0.0.1:9200 >/dev/null; then
    break
  fi
  sleep 2
done
curl -sf http://127.0.0.1:9200 >/dev/null
echo "$(date -Is) OpenSearch ready; starting chunked full-v1" | tee -a "$LOG"

set +e
/home/nevidomy/work/edrsr/.venv/bin/court-search ingest-full \
  --name full-v1 \
  --shards 4 \
  --chunk-gib 80 \
  2>&1 | tee -a "$LOG"
status=${PIPESTATUS[0]}
set -e
echo "$(date -Is) ingest-full exit=$status" | tee -a "$LOG"
exit "$status"
