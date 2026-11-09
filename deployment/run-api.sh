#!/usr/bin/env bash
# Serve REST + MCP on 127.0.0.1:8000 (put a TLS proxy such as Tailscale Serve/Funnel in front).
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
export COURT_SEARCH_CONFIG=${COURT_SEARCH_CONFIG:-$ROOT/config/local.yaml}
export HF_HOME=${HF_HOME:-$HOME/data/edrsr/models}
exec "$ROOT/.venv/bin/court-search" serve --host 127.0.0.1 --port "${COURT_SEARCH_PORT:-8000}"
