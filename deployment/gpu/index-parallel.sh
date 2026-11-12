#!/usr/bin/env bash
# Start N parallel loaders for one section as persistent user services
# (index-<section>-<k>of<N>). Each loads a size-balanced share of the section's shards.
# They resume after a crash (restart in 60 s) and after a reboot (enabled at boot);
# a loader whose share is done exits at once.
#
#   ./deployment/gpu/index-parallel.sh civil 4
#   FROM_DIR=~/data/edrsr/hf/data/civil ./deployment/gpu/index-parallel.sh civil 4   # HF download
#   systemctl --user list-units --all 'index-civil-*'     # status
#   systemctl --user stop 'index-civil-*'                 # pause (safe; start again to resume)
#   ./deployment/gpu/index-parallel.sh civil --remove     # when the section is done: disable + delete units
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
SECTION=${1:?usage: index-parallel.sh <section> <loaders|--remove>}
N=${2:?usage: index-parallel.sh <section> <loaders|--remove>}
CONFIG=${COURT_SEARCH_CONFIG:-$ROOT/config/local.yaml}
UNITS=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user

remove_units() {
  for unit in "$UNITS"/index-"$SECTION"-*of*.service; do
    [ -e "$unit" ] || continue
    name=$(basename "$unit")
    systemctl --user disable --now "$name" 2>/dev/null || true
    rm -f "$unit"
  done
  systemctl --user daemon-reload
}

if [ "$N" = "--remove" ]; then
  remove_units
  echo "removed loader units for $SECTION"
  exit 0
fi

if systemctl --user list-units --no-legend --state=active "index-$SECTION-*" | grep -q .; then
  echo "loaders for $SECTION are running; stop them first: systemctl --user stop 'index-$SECTION-*'" >&2
  exit 1
fi
# Fold earlier loaders' progress into os-index.json so a new split never re-sends finished files.
COURT_SEARCH_CONFIG="$CONFIG" "$ROOT/.venv/bin/python" -c \
  "from court_search.embed.index_os import merge_loader_reports as m; from court_search.runtime import load_config; import os; d=os.environ.get('FROM_DIR'); print(m(load_config(), section_id='$SECTION', from_dir=d or None))"
remove_units   # drop units from an earlier split (different N)
mkdir -p "$UNITS" "$ROOT/data/reports"
for k in $(seq 0 $(( N - 1 ))); do
  unit="index-$SECTION-${k}of$N"
  log="$ROOT/data/reports/$unit.log"
  cat > "$UNITS/$unit.service" <<EOF
[Unit]
Description=court-search: index $SECTION passages, share $k/$N
After=court-search.service

[Service]
Type=simple
WorkingDirectory=$ROOT
Environment=COURT_SEARCH_CONFIG=$CONFIG
Environment=PYTHONUNBUFFERED=1
ExecStart=$ROOT/.venv/bin/court-search index-passages --section $SECTION --shard $k/$N${FROM_DIR:+ --from-dir $FROM_DIR}
Nice=10
Restart=on-failure
RestartSec=60
StandardOutput=append:$log
StandardError=append:$log

[Install]
WantedBy=default.target
EOF
done
systemctl --user daemon-reload
for k in $(seq 0 $(( N - 1 ))); do
  systemctl --user enable --now "index-$SECTION-${k}of$N.service" 2>/dev/null
done
systemctl --user list-units --no-legend "index-$SECTION-*" | awk '{print $1, $3, $4}'
