# court-search

Search and read Ukrainian court decisions (ЄДРСР / EDRSR) through REST and MCP.

This is the application described in `IMPLEMENTATION_PLAN.md`, `API_AND_MCP.md`, and `DEPLOYMENT.md`.

## Quick start (local slice)

The project environment is already at `.venv`. You do not need `uv` on your PATH:

```bash
cd ~/work/edrsr
export COURT_SEARCH_CONFIG="$PWD/config/local.yaml"
.venv/bin/court-search init-secrets
.venv/bin/court-search ingest-slice --limit 400
.venv/bin/court-search serve
```

In another terminal:

```bash
export COURT_SEARCH_TOKEN="$(cat data/secrets/api.token)"
~/work/edrsr/.venv/bin/court-search smoke-test --api-url http://127.0.0.1:8000
```

`uv` lives at `~/.local/bin/uv` if you want it later (`export PATH="$HOME/.local/bin:$PATH"`).

MCP stdio bridge (Claude Desktop / other local hosts):

```json
{
  "mcpServers": {
    "ukrainian-court-search": {
      "command": "/home/nevidomy/work/edrsr/.venv/bin/court-search-mcp",
      "args": ["--api-url", "http://127.0.0.1:8000"],
      "env": {"COURT_SEARCH_TOKEN": "paste-token-from-data/secrets/api.token"}
    }
  }
}
```

See `docs/clients.md` for Claude Desktop and ChatGPT setup.

Raw Parquet stays at `~/data/edrsr/`. Derived files live under `data/` in this repository (gitignored).
