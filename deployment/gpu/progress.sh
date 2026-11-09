#!/usr/bin/env bash
# Embedding progress across the archive disk, this machine, an optional extra GPU box and a rental, plus indexed counts.
#   ./deployment/gpu/progress.sh            # snapshot
#   ./deployment/gpu/progress.sh --watch 300
# Rental address: data/state/rental-ssh (e.g. "-p 41234 root@1.2.3.4") or RENTAL_SSH.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/env.sh"
exec "$PYTHON" "$ROOT/deployment/gpu/progress.py" "$@"
