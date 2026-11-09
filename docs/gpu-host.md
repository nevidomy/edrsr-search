# Everything on one GPU machine

A complete walkthrough for a Linux or WSL2 machine with an NVIDIA GPU, with every part
enabled: full-text search over all ~95.7M decisions, semantic search over ~310M
passages, query encoding and reranking on the GPU, and (optionally) encoding passages
yourself. For the general reference see [operations.md](operations.md); this page is the
same path with the GPU parts spelled out and in order.

## What the GPU does

| when | model | VRAM (fp16) | without a GPU |
|---|---|---|---|
| every search | `BAAI/bge-m3` query encoder | together ~1.2 GB (measured, RTX 3060) | runs on CPU (slower per query) |
| every search, top results | `BAAI/bge-reranker-v2-m3` | | runs on CPU (slow), or `rerank: false` |
| optional, once | `BAAI/bge-m3` passage encoding | ~6.5 GB at batch 128, 1000 tokens | impractical |

The API picks the GPU automatically when `torch.cuda.is_available()`; models load on
the first search. Passage encoding speed, measured: **RTX 5090 ≈ 280 passages/s**
(≈310 GPU-hours for the whole corpus), **RTX 3060 12 GB ≈ 50/s**. Unless you want to
re-encode, use the [published vectors](https://huggingface.co/datasets/nevidomy/edrsr-search)
and keep the GPU for searching.

## Time and disk for a full build (measured on a 32-thread desktop)

| step | time | disk |
|---|---|---|
| download source parquet | depends on link (~100 GB) | ~100 GB |
| full-text index (`ingest-full`) | ~16.5 h for 95.8M decisions | ~750 GB |
| passage vectors: download | ~2.5 h at 100 MB/s, ~16 h at 14 MB/s (~800 GB) | per section, temporary |
| passage vectors: encode instead | ~13 days on one RTX 5090 | ~1 TB of shards |
| passage index (8 parallel loaders) | ~3–4 days for ~310M | ~1.3 TB |

With downloaded vectors the whole build is about 4–5 days, mostly unattended.

## 0. Check the GPU

```bash
nvidia-smi        # driver version, GPU, free memory
```

- **WSL2:** the Windows NVIDIA driver provides CUDA inside WSL (`/usr/lib/wsl/lib/nvidia-smi`).
  Do not install a Linux NVIDIA driver inside WSL.
- **RTX 50xx (Blackwell):** needs CUDA 12.8+ PyTorch wheels and a driver ≥ 570.

## 1. Install with CUDA PyTorch

```bash
git clone https://github.com/nevidomy/edrsr-search && cd edrsr-search
python3 -m venv .venv
.venv/bin/pip install torch --index-url https://download.pytorch.org/whl/cu128   # cu126 for older drivers
.venv/bin/python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
.venv/bin/pip install -e '.[gpu,dev]'
```

The second line must print `True` and your GPU's name. "no kernel image is available"
later means the torch build is too old for the GPU (use a newer CUDA wheel).

## 2. Models

```bash
export HF_HOME=~/data/edrsr/models          # same as storage.models in the config
.venv/bin/hf download BAAI/bge-m3
.venv/bin/hf download BAAI/bge-reranker-v2-m3
```

The published vectors were made with bge-m3 revision `5617a9f` (also `main`; see
`config/pins.yaml`). Queries must use the same model, or semantic results degrade.
`deployment/run-api.sh` uses this `HF_HOME` by default.

## 3. Configuration, secrets, OpenSearch

```bash
cp config/example.yaml config/local.yaml     # paths; keep search.backend: opensearch
cp deployment/env.example deployment/.env    # OPENSEARCH_DATA / OPENSEARCH_SNAPSHOTS on the big fast disk
.venv/bin/court-search init-secrets           # API token -> data/secrets/api.token
./deployment/install-docker-engine.sh         # once, if Docker is missing (Ubuntu/WSL)
./deployment/ensure-opensearch.sh             # builds the OpenSearch 3.8 + Ukrainian analyzer image, starts it
.venv/bin/court-search doctor
```

OpenSearch runs on the CPU; it does not use the GPU. Give it a local SSD or NVMe disk:
vector search does many small random reads.

## 4. Source data

```bash
for d in tspk kas kpk gpk kupap; do
  .venv/bin/hf download overthelex/edrsr-$d --repo-type dataset --local-dir ~/data/edrsr/edrsr-$d
done
.venv/bin/court-search sources
```

Log in first (`.venv/bin/hf auth login`, a read token) to avoid anonymous rate limits.

## 5. Full-text index

```bash
tmux new -s ingest '.venv/bin/court-search ingest-full --name full-v1; bash'
# when it finishes:
.venv/bin/court-search restore-archive --name full-v1 --activate
```

Resumable: re-run the same command after any interruption. Progress is in
`data/reports/` and `<storage.archive>/full-v1-status.json`.

## 6. Passage vectors

### 6a. Download the published vectors (recommended)

```bash
SECTIONS="commercial" ./deployment/gpu/rental/pull-from-hf.sh       # -> ~/data/edrsr/hf/data/commercial
FROM_DIR=~/data/edrsr/hf/data/commercial ./deployment/gpu/index-parallel.sh commercial 4
```

Do one section at a time (`commercial`, `criminal`, `administrative_offense`, `civil`,
`administrative`) and delete each download once its loaders finish:

```bash
systemctl --user list-units 'index-*'           # loaders still running?
tail -f data/reports/index-commercial-0of4.log
```

### 6b. Or encode on this GPU

```bash
./deployment/gpu/probe-batch.sh civil          # finds the largest batch that fits; saved for later runs
tmux new -s embed './deployment/gpu/run-section.sh civil; bash'    # one GPU, resumable per file
```

Several GPUs in this machine (one worker per GPU, files claimed atomically):

```bash
SECTIONS="civil administrative" DATA_ROOT=~/data/edrsr SKIP_DOWNLOAD=1 BATCH_SIZE=128 \
  ./deployment/gpu/rental/run-multi.sh
```

If `nvidia-smi` shows the GPU dropping to 0% between files (CPU-bound chunking on slower
CPUs), add a second worker per GPU without stopping anything:
`DATA_ROOT=~/data/edrsr SECTIONS="civil administrative" ./deployment/gpu/rental/add-workers.sh`.
Then index each finished section: `./deployment/gpu/index-parallel.sh civil 4`.

Watch everything (encoded, indexed, rates, GPU use, ETA) with
`./deployment/gpu/progress.sh --watch 60`, or as a page:
`./deployment/systemd/install.sh embed-progress && systemctl --user enable --now embed-progress`
(http://127.0.0.1:8090).

## 7. Serve with the GPU

```bash
./deployment/systemd/install.sh                    # court-search.service for this checkout
systemctl --user enable --now court-search.service
sudo loginctl enable-linger "$USER"
curl -s http://127.0.0.1:8000/health/ready         # {"status":"ready","generation":"full-v1"}
COURT_SEARCH_TOKEN="$(cat data/secrets/api.token)" .venv/bin/court-search smoke-test   # also loads the models
journalctl --user -u court-search | grep "loading BAAI"   # "... on cuda fp16"
nvidia-smi                                          # GPU memory use rises by ~1–2 GB
```

**Sharing the GPU** between serving and encoding works when there is room (encoding at batch 128 needs ~6.5 GB more).
Otherwise run the API on the CPU while encoding: add
`Environment=CUDA_VISIBLE_DEVICES=` to `~/.config/systemd/user/court-search.service`,
then `systemctl --user daemon-reload && systemctl --user restart court-search`
(remove it afterwards). Several GPUs: pin the API to one with `CUDA_VISIBLE_DEVICES=0`
and give the encoders `GPUS="1 2 3"`.

## 8. Connect Claude

See [clients.md](clients.md): Claude Code (`claude mcp add --transport http ...`),
Claude Desktop (stdio bridge), or the claude.ai connector over public HTTPS (e.g.
Tailscale Funnel).

## GPU troubleshooting

| symptom | fix |
|---|---|
| `torch.cuda.is_available()` is False | wrong torch build (CPU wheel) or driver too old; reinstall from the CUDA index URL |
| "no kernel image is available for execution on the device" | torch built for an older CUDA than the GPU needs (Blackwell: cu128+) |
| CUDA out of memory while encoding | lower `BATCH_SIZE` (or re-run `probe-batch.sh`); stop the API or move it to CPU |
| GPU utilisation saw-tooths to 0% | CPU-bound chunking: `add-workers.sh` (second worker per GPU) |
| `nvidia-smi` hangs on WSL under heavy load | usually clears by itself; encoding continues; restart WSL after the run if not |
| search loads models on CPU despite a GPU | the API runs in an environment without CUDA torch: check `.venv` and the unit's `ExecStart` |
