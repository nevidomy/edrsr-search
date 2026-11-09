# edrsr-search

Search and read Ukrainian court decisions from the Unified State Register of Court
Decisions (ЄДРСР / EDRSR) through a REST API and an **MCP server** that Claude and other
assistants can use as a tool.

- **Corpus:** ~95.7M decisions across five jurisdictions (civil, administrative,
  criminal, commercial, administrative offences), from the CC-BY-4.0
  [`overthelex/edrsr-*`](https://huggingface.co/datasets/overthelex/edrsr-tspk) datasets.
- **Hybrid retrieval:** OpenSearch BM25 with a Ukrainian analyzer over full decisions,
  plus semantic search over ~310M passages embedded with
  [BAAI/bge-m3](https://huggingface.co/BAAI/bge-m3), fused with reciprocal-rank fusion,
  with optional reranking (bge-reranker-v2-m3).
- **Vectors on Hugging Face:** every passage with its embedding is published at
  [**nevidomy/edrsr-search**](https://huggingface.co/datasets/nevidomy/edrsr-search),
  so you can build the semantic index without a GPU.
- **MCP tools:** `search_decisions`, `get_decision`, `get_case_decisions`,
  `lookup_filters`, `get_corpus_info` (read-only). Results cite EDRSR document ids and links.

```
Claude / other MCP host ──► /mcp (Streamable HTTP, OAuth or bearer token)
                         ──► court-search-mcp (stdio bridge) ──► REST /api/v1
                                   │
                      court-search API (FastAPI)
                       ├── OpenSearch: decisions-* (BM25)  +  passages-bge-m3-dense-v1 (HNSW)
                       ├── bge-m3 query encoder, optional reranker
                       └── SQLite state (generations, filters)
```

## Quick start: a small slice, no Docker, no GPU

```bash
git clone https://github.com/nevidomy/edrsr-search && cd edrsr-search
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
cp config/example.yaml config/local.yaml        # set search.backend: local_fts for this try
.venv/bin/hf download overthelex/edrsr-gpk --repo-type dataset --local-dir ~/data/edrsr/edrsr-gpk \
  --include 'data/edrsr-00[0-2]*' README.md     # a few files are enough for the slice
.venv/bin/court-search init-secrets              # writes data/secrets/api.token
.venv/bin/court-search ingest-slice --limit 400
.venv/bin/court-search serve                     # REST + MCP on 127.0.0.1:8000
```

In another terminal:

```bash
export COURT_SEARCH_TOKEN="$(cat data/secrets/api.token)"
.venv/bin/court-search smoke-test --api-url http://127.0.0.1:8000
```

## Documentation

| | |
|---|---|
| [docs/clients.md](docs/clients.md) | Connect Claude Desktop, Claude Code, the claude.ai connector, and other hosts |
| [docs/operations.md](docs/operations.md) | Full install: OpenSearch, the full corpus, the semantic index from Hugging Face, serving, updates, backups |
| [deployment/gpu/rental/README.md](deployment/gpu/rental/README.md) | Encode passages yourself on rented GPUs (≈230 RTX-5090-hours for the remaining sections) |
| [docs/design/](docs/design/) | Original design: [implementation plan](docs/design/IMPLEMENTATION_PLAN.md), [API and MCP contracts](docs/design/API_AND_MCP.md), [deployment runbook](docs/design/DEPLOYMENT.md) |

## Repository layout

```
src/court_search/     api (REST, OAuth), mcp (server, stdio bridge), retrieval, indexing,
                      ingest (source parquet -> normalized -> OpenSearch), embed (chunk, encode, index)
config/               example.yaml, dictionaries (courts, categories, judgment types)
contracts/            exported OpenAPI and tool schemas
deployment/           docker-compose + OpenSearch image, run/start scripts, systemd templates,
                      gpu/ (encoding, progress page, parallel index loading), hf/ (dataset publishing)
docs/                 client setup, operations, design
tests/
```

Machine-specific files stay out of git: `config/local.yaml`, `deployment/.env`, `data/`,
and `local/`.

## License

Code: [Apache-2.0](LICENSE). Data: the source decisions and the published passages and
vectors are CC-BY-4.0; credit the EDRSR register (reyestr.court.gov.ua) and the
`overthelex/edrsr-*` datasets. Search results are text similarity, not legal advice.
