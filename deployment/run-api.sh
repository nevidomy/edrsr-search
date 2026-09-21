#!/usr/bin/env bash
set -euo pipefail
cd /home/nevidomy/work/edrsr
export COURT_SEARCH_CONFIG=/home/nevidomy/work/edrsr/config/local.yaml
exec /home/nevidomy/work/edrsr/.venv/bin/court-search serve --host 127.0.0.1 --port 8000
