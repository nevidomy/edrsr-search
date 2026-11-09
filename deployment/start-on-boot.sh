#!/usr/bin/env bash
# Bring the service up after login/boot (e.g. from Windows Startup for WSL).
# Uses the systemd user unit when available, otherwise a tmux session.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
export XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}
export DBUS_SESSION_BUS_ADDRESS=${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}
if [ -S "$XDG_RUNTIME_DIR/bus" ] && command -v systemctl >/dev/null; then
  systemctl --user start court-search.service
  exit 0
fi
"$ROOT/deployment/ensure-opensearch.sh"
if pgrep -f 'court-search serve --host 127.0.0.1' >/dev/null; then
  exit 0
fi
unset TMUX_TMPDIR
if ! tmux has-session -t '=court-search' 2>/dev/null; then
  tmux new-session -d -s court-search "$ROOT/deployment/run-api-tmux.sh"
  tmux set-option -t court-search remain-on-exit on
fi
