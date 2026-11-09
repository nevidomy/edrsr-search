#!/usr/bin/env bash
# Render the unit templates for this checkout and install them as systemd *user* units.
#   ./deployment/systemd/install.sh                 # court-search.service
#   ./deployment/systemd/install.sh embed-progress  # progress page, bound to localhost (+ tailnet IP if any)
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
DEST=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
mkdir -p "$DEST"
units=${*:-court-search}
bind=127.0.0.1:8090
if command -v tailscale >/dev/null && ip=$(tailscale ip -4 2>/dev/null | head -1) && [ -n "$ip" ]; then
  bind="$bind,$ip:8090"
fi
for unit in $units; do
  sed -e "s#@ROOT@#$ROOT#g" -e "s#@BIND@#$bind#g" "$ROOT/deployment/systemd/$unit.service" > "$DEST/$unit.service"
  echo "installed $DEST/$unit.service"
done
systemctl --user daemon-reload
echo "enable with: systemctl --user enable --now <unit>   (and: sudo loginctl enable-linger \$USER)"
