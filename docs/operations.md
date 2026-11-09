# Operations: full install, updates, backups

This sets up the whole corpus: OpenSearch with full-text search over every decision,
plus semantic search over passages, served over REST and MCP. For a five-minute try on a
few hundred decisions, use the quick start in the [README](../README.md) instead.

## What you need

| | full corpus | notes |
|---|---|---|
| CPU / RAM | 16+ cores, 48+ GB RAM | OpenSearch heap 12 GB in a 20 GB container by default |
| Source parquet | ~100 GB | `overthelex/edrsr-*`, five datasets |
| Decisions index (BM25) | ~750 GB | built from the source parquet |
| Passage index (HNSW, float32) | ~1.3 TB for ~310M passages | ~4.1 KB per passage |
| Passage vectors | ~800 GB download | only while indexing; one section at a time works |
| GPU | not required | needed only to encode passages yourself; CPU torch is enough for query encoding |

Local SSD/NVMe matters for search latency: vector search does many small random reads.
On WSL, a Windows drive mounted under `/mnt/<letter>` works but is slow for this; prefer
a disk WSL mounts as ext4.

## 1. Install

```bash
git clone https://github.com/nevidomy/edrsr-search && cd edrsr-search
python3 -m venv .venv
.venv/bin/pip install torch --index-url https://download.pytorch.org/whl/cpu   # skip if you have CUDA torch
.venv/bin/pip install -e '.[gpu,dev]'      # gpu extra = sentence-transformers for bge-m3 and the reranker
cp config/example.yaml config/local.yaml   # then edit paths
cp deployment/env.example deployment/.env  # OpenSearch data/snapshot dirs, heap
.venv/bin/court-search init-secrets         # API token in data/secrets/
```

Docker Engine is needed for OpenSearch (`deployment/install-docker-engine.sh` installs it on
Ubuntu/WSL). Then:

```bash
./deployment/ensure-opensearch.sh          # builds the image (OpenSearch 3.8 + Ukrainian analyzer) and waits for it
.venv/bin/court-search doctor              # checks config, sources, OpenSearch, secrets
```

## 2. Source data

```bash
for d in tspk kas kpk gpk kupap; do
  .venv/bin/hf download overthelex/edrsr-$d --repo-type dataset --local-dir ~/data/edrsr/edrsr-$d
done
.venv/bin/court-search sources              # what each configured source contains
```

Anonymous Hugging Face downloads are rate-limited; `hf auth login` with a read token avoids it.

## 3. Full-text index (decisions)

```bash
.venv/bin/court-search ingest-full --name full-v1      # resumable; progress in data/reports/
.venv/bin/court-search restore-archive --name full-v1 --activate
```

`ingest-full` normalizes every source file and indexes decisions into OpenSearch in
~80 GiB chunks (`--chunk-gib`). Each sealed chunk is snapshotted into
`OPENSEARCH_SNAPSHOTS` and dropped from the live cluster, so the build fits a smaller fast
disk; `restore-archive` restores all chunks into `OPENSEARCH_DATA` and makes the
generation active. Re-running either command skips finished work. Limit to some
jurisdictions with `--jurisdiction civil` (repeatable).

## 4. Semantic index (passages)

### From the published vectors (no GPU)

```bash
SECTIONS="commercial" ./deployment/gpu/rental/pull-from-hf.sh     # -> ~/data/edrsr/hf/data/commercial/
FROM_DIR=~/data/edrsr/hf/data/commercial ./deployment/gpu/index-parallel.sh commercial 4
```

Repeat per section (`civil`, `administrative`, `criminal`, `commercial`,
`administrative_offense`); delete a section's download once it is indexed. The loaders
are user services (`systemctl --user list-units 'index-*'`), resume after a stop or
reboot, and never re-send finished files. Measured on a 32-thread desktop: ~300
passages/s with one loader, ~1,100/s with eight, so ~3–4 days for all ~310M.

### Encoding yourself

`court-search embed-passages --section <s> --batch-size 128` on a local GPU (~280
passages/s on an RTX 5090), or on rented GPUs with
[deployment/gpu/rental](../deployment/gpu/rental/README.md). Then
`./deployment/gpu/index-parallel.sh <section> 4`.

The passage index stores the vector plus `doc_id` and character offsets; passage text is
read from the decisions index, so step 3 must cover the same sections.

## 5. Serve

```bash
./deployment/systemd/install.sh                 # renders court-search.service for this checkout
systemctl --user enable --now court-search.service
sudo loginctl enable-linger "$USER"             # keep user services running without a login
curl -s http://127.0.0.1:8000/health/ready      # {"status":"ready","generation":"full-v1"}
```

The API listens on `127.0.0.1:8000` only. Put an HTTPS proxy in front for other devices
(e.g. `tailscale serve --bg 8000` for your tailnet, or `tailscale funnel --bg 8000` for the
claude.ai connector) and list its host name in `api.allowed_hosts`. Client setup:
[clients.md](clients.md). On WSL without systemd, `deployment/start-on-boot.sh` starts a
tmux session instead; `deployment/start-court-search-on-login.cmd` runs it from Windows
Startup.

### Progress page

`./deployment/gpu/progress.sh` prints, and `./deployment/systemd/install.sh embed-progress`
serves on `127.0.0.1:8090` (plus the tailnet IP), what is encoded where, what is
indexed, live rates and ETA. Other encoding machines: `EXTRA_GPU_SSH`, `RENTAL_SSH`
(see the script header).

## 6. Updating the corpus

New source snapshots are imported deliberately, not polled:

1. Download the new dataset revisions next to the old ones (or into new folders) and
   point `config/local.yaml` at them.
2. `court-search ingest-full --name full-v2`, then `restore-archive --name full-v2`.
3. Encode and index passages for new or changed decisions.
4. `court-search activate full-v2`. Roll back with `court-search activate full-v1`
   while the old indices still exist.

## 7. Backups and recovery

- **Index:** OpenSearch snapshots land in `OPENSEARCH_SNAPSHOTS` (`ingest-full` makes
  one per chunk). They restore only into a compatible OpenSearch version (3.8 with the
  Ukrainian analyzer image from `deployment/opensearch`).
- **Vectors:** the published dataset is the backup; re-index from it at any time.
- **Secrets:** `data/secrets/` (API token and its hash). Rotating: `init-secrets`, then
  reconnect clients.
- **Rebuild from scratch:** source parquet + this repo + the vectors dataset are enough.

## 8. Publishing vectors (maintainers)

`deployment/hf/publish_embeddings.py --repo <user>/<name> --public` converts encoded
shards to the published format (float32 `vector`, internal columns dropped), uploads in
verified batches, and resumes from what is already on the Hub. The dataset card is
`deployment/hf/dataset_card.md`. Private repos of this size need a paid Hub plan.

## Troubleshooting

| symptom | cause / fix |
|---|---|
| claude.ai connector shows "down" (502) | the public URL changed (e.g. Tailscale machine renamed) or the API is stopped; check `/health/ready` locally and the URL in the connector |
| 429 from Hugging Face | anonymous rate limit: log in with a read token, or copy data in from another machine |
| model "couldn't find them in the cached files" on a rented box | the provider preset `HF_HOME`; the GPU launcher pins `$DATA_ROOT/models` (`EMBED_HF_HOME` to override) |
| slow vector search | index on a slow or network disk, or too little RAM for the page cache; move `OPENSEARCH_DATA` to local NVMe |
| semantic results missing, `semantic_*_fallback_lexical` warnings | `torch`/`sentence-transformers` not installed, or no passages indexed for those sections |
