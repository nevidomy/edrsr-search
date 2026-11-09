#!/usr/bin/env bash
# Start (or keep) the OpenSearch container and wait until it answers on 127.0.0.1:9200.
# Paths and sizes: deployment/.env (see deployment/env.example).
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT/deployment"
docker compose -f docker-compose.yml up -d
for i in $(seq 1 120); do
  if curl -sf --max-time 2 http://127.0.0.1:9200 >/dev/null; then
    exit 0
  fi
  sleep 2
done
echo "OpenSearch did not become reachable on 127.0.0.1:9200" >&2
exit 1
