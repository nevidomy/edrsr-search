# Shared by the GPU shell wrappers. Repo may be named edrsr, edrsr-search, etc.
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
export COURT_SEARCH_CONFIG=${COURT_SEARCH_CONFIG:-$ROOT/config/local.yaml}
export HF_HOME=${HF_HOME:-$HOME/data/edrsr/models}

if [ -x "$ROOT/.venv/bin/court-search" ]; then
  PYTHON="$ROOT/.venv/bin/python"
  COURT_SEARCH="$ROOT/.venv/bin/court-search"
elif [ -n "${VIRTUAL_ENV:-}" ] && [ -x "$VIRTUAL_ENV/bin/court-search" ]; then
  PYTHON="$VIRTUAL_ENV/bin/python"
  COURT_SEARCH="$VIRTUAL_ENV/bin/court-search"
elif command -v court-search >/dev/null && command -v python3 >/dev/null; then
  PYTHON=python3
  COURT_SEARCH=court-search
else
  echo "court-search is not installed in this checkout ($ROOT)." >&2
  echo "Once:" >&2
  echo "  cd $ROOT" >&2
  echo "  python3 -m venv .venv" >&2
  echo "  .venv/bin/pip install -U pip" >&2
  echo "  .venv/bin/pip install -e '.[gpu]'" >&2
  exit 1
fi
mkdir -p "$HOME/data/edrsr/embeddings" "$HF_HOME"
SETTINGS=${SETTINGS:-$HOME/data/edrsr/embeddings/settings.json}

read_setting() {
  local key=$1 default=$2
  if [ ! -f "$SETTINGS" ]; then
    echo "$default"
    return
  fi
  "$PYTHON" -c "import json; print(json.load(open(r'''$SETTINGS''')).get('$key', $default))"
}

save_settings() {
  local batch=$1 length=$2
  "$PYTHON" -c "
import json
from datetime import UTC, datetime
from pathlib import Path
path = Path(r'''$SETTINGS''')
data = {}
if path.exists():
    data = json.loads(path.read_text())
data.update({
    'batch_size': int($batch),
    'max_length': int($length),
    'updated_at': datetime.now(UTC).isoformat(),
})
path.write_text(json.dumps(data, indent=2) + '\n')
print(path)
"
}
