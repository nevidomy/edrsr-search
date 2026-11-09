# Encoding passages on rented GPUs

`run-multi.sh` turns any CUDA box (1–8+ GPUs) into a passage encoder. It:

1. downloads the source sections from Hugging Face;
2. starts one encoder worker per GPU, with each worker claiming files on its own;
3. lets home pull finished shards over SSH while it runs (or rsyncs them home).

Work resumes per source file. If the box restarts or a worker crashes, run the
script again and it picks up where it stopped.

## Throughput reference (bge-m3, fp16, max_length 1000, batch 128)

| GPU | passages/s (one worker) | notes |
|---|---|---|
| RTX 5090 | 260–340 | measured on commercial/criminal/administrative_offense/civil |
| RTX 3060 12 GB | ~50 | batch 32 |
| H100 | not measured | expect 2–3× a 5090; run a probe before committing |

Workers per GPU: on a desktop 5090 (fast CPU) one worker keeps the GPU busy
(two measured 282/s against 277/s). On a rented 8× 5090 box with an EPYC 7K62 the
GPUs idled while each worker chunked its next file on the slower CPU; a second
worker per GPU (`add-workers.sh`, no restart) brought them to 100% and added ~10%.

Whole corpus, all five sections: ~95.7M decisions → ~310M passages (civil 102.5M,
administrative 91.7M, criminal 54.3M, commercial 32.8M, administrative offences
29.0M), about **300 RTX-5090-hours** and ~1 TB of shards. Measured on a rented
8× RTX 5090 box (host power cap 480 W per card): ~2,300 passages/s, i.e. 163.7M
passages in 19.6 h, so ~37 h for everything. You only need this to re-encode: the
results are published at
[nevidomy/edrsr-search](https://huggingface.co/datasets/nevidomy/edrsr-search).

## Picking a machine (Vast.ai)

Vast.ai is a marketplace and is usually the cheapest place to rent 5090s.
Filters that matter:

| filter | value | why |
|---|---|---|
| GPU | RTX 5090 ×8 (or several smaller boxes, see below) | |
| Max CUDA | ≥ 12.8 | Blackwell needs CUDA 12.8 wheels |
| Disk | ≥ 1.2 TB (set at creation, cannot grow later) | 60 GB input + ~650 GB output + models |
| CPU cores | ≥ 64 (≥ 8 per GPU) | tokenizing runs on the CPU |
| RAM | ≥ 128 GB | |
| Inet up | ≥ 1 Gbps | shards leave the box while it runs |
| Reliability | ≥ 99 %, verified/datacenter | |
| Type | on-demand | interruptible is cheaper and resumable, but pauses can wipe out the savings |

CLI equivalent (after `pip install vastai; vastai set api-key ...`):

```bash
vastai search offers 'gpu_name=RTX_5090 num_gpus=8 cuda_vers>=12.8 disk_space>=1200 \
  cpu_cores_effective>=64 inet_up>=1000 reliability>0.99 verified=true rentable=true' -o 'dph'
vastai create instance <OFFER_ID> --image pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime \
  --disk 1200 --ssh --direct
```

RunPod (Community or Secure Cloud) works the same way: pick a PyTorch CUDA 12.8+
template and a volume of at least 1.2 TB.

Several smaller boxes work too: give each the same `SECTIONS`, its own
`MACHINE_SHARD=k/n`, and the same upload target. Files are split by size, so
the boxes finish at about the same time.

## Run

```bash
# from home: copy the code (no data, no secrets) to the box
rsync -a --exclude .venv --exclude data --exclude .git ./ root@<box>:/workspace/edrsr-search/
#   (or `git clone` once the repo is public)

# on the rented box
cd /workspace/edrsr-search
export SECTIONS="civil administrative"
export DATA_ROOT=/workspace/edrsr
export HF_TOKEN=hf_...                         # read-only token: anonymous downloads get rate-limited (429)
export BATCH_SIZE=128                          # known good on 32 GB; omit to probe
tmux new -s embed './deployment/gpu/rental/run-multi.sh 2>&1 | tee -a $DATA_ROOT/logs/run.log'

# progress / ETA, any time (on the box)
DATA_ROOT=/workspace/edrsr SECTIONS="civil administrative" ./deployment/gpu/rental/status.sh
```

Shipping options (use any):

- **Home pulls over SSH** (simplest; no token, nothing reaching into home).
  Add your home public key to the provider account, leave `SYNC_DEST` unset, and
  at home run
  `SSH_PORT=<port> ./deployment/gpu/rental/pull-from-rental.sh root@<box-ip> --loop`.
  Output averages ~50 Mbit/s over a 30 h run, so a 500 Mbit/s link on each end is plenty.
- **rsync home** (`SYNC_DEST=user@host:/path/to/embeddings/bge-m3-dense-v1/`).
  This needs an ssh key on the box that your home accepts, plus Tailscale
  (`TS_AUTHKEY`, an ephemeral key) or another route in.

When the run ends the box keeps its data. Stop or destroy it only after
`status.sh` shows every file done and a final pull at home has been checked:
same files, same sizes, and every shard opens with its recorded passage count.

## At home

`./deployment/gpu/progress.sh [--watch 300]` shows every section: files and passages
encoded, where the vectors are (archive disk, this machine, an extra GPU box, the rental), how much
is already at home, what OpenSearch has indexed, live rate, and ETA. Put the rental's
SSH arguments in `data/state/rental-ssh` (e.g. `-p 41234 root@1.2.3.4`) so it is included.

Load the shards into OpenSearch with parallel loaders (each resumes on restart):

```bash
./deployment/gpu/index-parallel.sh civil 4
./deployment/gpu/index-parallel.sh administrative 4
```

Revoke the HF token used on the rental box once the run is over.

### If the anonymous Hugging Face download gets rate-limited

Push the source parquet and the model cache from a machine that already has them
(`rsync --size-only` skips files already there), then run with `SKIP_DOWNLOAD=1
HF_HUB_OFFLINE=1 WAIT_FOR_INPUTS=1` and `touch $DATA_ROOT/.inputs-complete` once the
copy is done. Workers start on the files present and pick up new ones as they land.
Some providers preset `HF_HOME`; the launcher always uses `$DATA_ROOT/models`
(override with `EMBED_HF_HOME`).
