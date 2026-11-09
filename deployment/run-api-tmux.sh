#!/usr/bin/env bash
# Wait for local OpenSearch, then serve. For a tmux session when systemd is not available.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
echo "$(date -Is) waiting for OpenSearch on 127.0.0.1:9200"
for i in $(seq 1 120); do
  if curl -sf --max-time 2 http://127.0.0.1:9200 >/dev/null; then
    echo "$(date -Is) OpenSearch is up"
    break
  fi
  sleep 2
done
curl -sS --max-time 5 http://127.0.0.1:9200/_cluster/health?pretty || true
exec "$ROOT/deployment/run-api.sh"
