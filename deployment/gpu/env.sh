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
