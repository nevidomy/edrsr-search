#!/usr/bin/env bash
# Copy the live OpenSearch data dir from the WSL Docker volume onto F:,
# then start OpenSearch from docker-compose.archive.yml. Pilot stays searchable.
set -euo pipefail
ROOT=/home/nevidomy/work/edrsr
COMPOSE="$ROOT/deployment/docker-compose.yml"
ARCHIVE="$ROOT/deployment/docker-compose.archive.yml"
DEST=/mnt/f/edrsr/opensearch
SNAP=/mnt/f/edrsr/snapshots
mkdir -p "$DEST" "$SNAP"
chmod a+rwx /mnt/f/edrsr "$DEST" "$SNAP" || true

docker_cmd() {
  if command -v sg >/dev/null && getent group docker >/dev/null; then
    sg docker -c "$*"
  else
    bash -lc "$*"
  fi
}

echo "stopping OpenSearch (brief search blip)"
docker_cmd "docker compose -f $COMPOSE stop opensearch" || true

if [ -d "$DEST/nodes" ]; then
  echo "F: already has OpenSearch nodes; skipping copy"
else
  echo "copying Docker volume deployment_opensearch-data -> $DEST"
  docker_cmd "docker run --rm -v deployment_opensearch-data:/from:ro -v $DEST:/to alpine:3.20 sh -c 'cp -a /from/. /to/'"
fi

echo "starting OpenSearch with data on F:"
docker_cmd "docker compose -f $COMPOSE down --remove-orphans" || true
docker_cmd "docker compose -f $ARCHIVE up -d"

for _ in $(seq 1 90); do
  if curl -sf http://127.0.0.1:9200 >/dev/null; then
    break
  fi
  sleep 2
done
curl -sf http://127.0.0.1:9200 >/dev/null
curl -sS 'http://127.0.0.1:9200/_cat/indices?v'
echo "OpenSearch is serving from $DEST"
