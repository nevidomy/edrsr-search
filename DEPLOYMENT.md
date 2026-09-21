# Deployment and operation

Companion to the [implementation plan](IMPLEMENTATION_PLAN.md) and [API/MCP contracts](API_AND_MCP.md). This is the runbook the implementation should deliver. Application command names and configuration keys below are proposed interfaces, not an already released package.

## 1. First host: Windows 11 and WSL2

Verify the target machine rather than using the machine where this plan was written. Confirm WSL2, available Linux memory, actual dedicated GPU memory, available disk inside the Linux filesystem, and which machine/distro currently owns the Tailscale endpoint. Record the existing network routes before adding this application.

Use a single Docker installation: either Docker Desktop with WSL integration or Docker Engine inside the chosen distro. Avoid accidentally running two separate engines and putting the application data in the wrong one. Keep application indexes, SQLite, caches, and temporary files on the Linux filesystem. Microsoft recommends Linux-side storage for Linux workloads instead of working across `/mnt/c`. [WSL filesystem guidance](https://learn.microsoft.com/en-us/windows/wsl/filesystems)

The existing raw Parquet files can remain at their downloaded location and be mounted read-only. If cross-filesystem reads become an ingestion bottleneck, copy a selected input scope into Linux storage after forecasting the extra space. Do not move or overwrite the only original copy.

WSL memory is configurable and may expose less than the host's full RAM. For this 64 GB host, use approximately 48 GB as an initial WSL ceiling if compatible with the user's other workloads. Keep Windows headroom. Check the distro virtual disk limit as well as physical free space. WSL configuration changes apply to the VM and may require restarting running distributions. [WSL configuration reference](https://learn.microsoft.com/en-us/windows/wsl/wsl-config)

### Initial resource budget

| Component | Starting budget | Adjustment rule |
|---|---|---|
| OpenSearch container | Around 28 GB RAM limit; 12 GB JVM heap | Leave room inside its limit for native vector work and mapped/index pages; tune from measured memory and latency |
| API/MCP container | 1–2 GB | One application worker initially; avoid duplicating loaded models |
| Model worker | Up to 8 GB system RAM initially, plus measured GPU allocation | Tune batch size from actual VRAM and sequence lengths; increase only within the host budget |
| Ingest process | 4 GB bounded buffers initially | Disk-backed operations and bounded queues; do not retain entire datasets |
| Remaining WSL capacity | Approximately 6 GB or more | OS, other processes, transient allocations, and additional page-cache headroom |

These are starting limits to benchmark, not guaranteed requirements. Avoid running the largest bulk embedding job and peak interactive reranking simultaneously. Pause or throttle bulk inference when interactive jobs arrive.

OpenSearch's container host needs the documented virtual-memory-map setting, suitable resource limits, persistent storage, and explicit credentials. Check the requirements for the pinned release rather than copying a demo configuration with security disabled. [OpenSearch Docker installation](https://docs.opensearch.org/latest/install-and-configure/install-opensearch/docker/)

For CUDA under WSL, use the compatible Windows NVIDIA driver and verify access from both WSL and the inference container. Do not install a Linux NVIDIA display driver over the WSL driver interface. [NVIDIA CUDA on WSL guide](https://docs.nvidia.com/cuda/wsl-user-guide/index.html)

## 2. Compose services and storage

| Service | Always running? | Exposed outside Compose? |
|---|---|---|
| `opensearch` | Yes | No database port published publicly |
| `api` with REST and HTTP MCP | Yes | Loopback application port, proxied by the chosen private endpoint |
| `model-worker` | Yes for warm semantic search; optional for lexical mode | Internal network only |
| `ingest` | One-shot operator job | No |
| `mcp-bridge` | Started by each client as needed | No listening port required |

Use Compose profiles/overrides named `cpu` and `cuda` with the same internal model-worker interface. The CUDA profile supplies the compatible inference image and GPU reservation. The CPU profile runs the same embedding specification without a GPU; expect lower throughput and possibly fewer reranked candidates to meet latency targets. Any deliberately smaller candidate budget must appear in the retrieval configuration/version.

Use a small production API image, a separate inference image, and a custom OpenSearch image containing the matching Ukrainian analyzer plugin. Pin image versions/digests, Python dependencies, and model revisions. Keep an explicit compatibility file listing these pins. Build and test for the target CPU architecture; do not assume every CUDA image or search plugin is available on every architecture.

Inside containers use stable paths:

```text
/data/raw/                 existing Parquet, read only
/data/normalized/          canonical decisions and audit partitions
/data/chunks/              source spans and model-input partitions
/data/embeddings/          content-addressed embedding shard cache
/data/state/               SQLite job/corpus database
/data/reports/             import, evaluation, and capacity reports
/data/snapshots/           search snapshot repository or mounted backup target
/models/                  pinned locally available model weights
```

Mount the actual raw directories through configuration. Keep SQLite on a local Linux volume, not a network filesystem. Configure a busy timeout and short transactions; bulk data belongs in files. Use the SQLite backup API for a consistent backup while the service is running.

Give the API search/read credentials and the operator job separate index-management credentials. Provide credentials via protected environment files or mounted secrets. Keep search database access inside the service network. The API image should not carry the original dataset download credentials.

## 3. Proposed configuration shape

The implementation should validate this configuration at startup, including path access, required model revisions, and vector-dimension compatibility. Placeholder revision values must fail validation until replaced by resolved pins.

```yaml
sources:
  - repo: overthelex/edrsr-tspk
    path: /data/raw/edrsr-tspk
    revision: null
  # Add kas, kpk, gpk, and kupap roots when present.

storage:
  normalized: /data/normalized
  chunks: /data/chunks
  embeddings: /data/embeddings
  state_db: /data/state/court-search.sqlite

search:
  url: https://opensearch:9200
  credentials_file: /run/secrets/opensearch-reader
  ca_file: /run/secrets/opensearch-ca.pem
  lexical_candidates: 100
  vector_passage_candidates: 300
  fusion_k: 60
  rerank_decisions: 50
  passages_per_decision: 2
  request_timeout_seconds: 30

embedding:
  model: BAAI/bge-m3
  revision: RESOLVE_AND_PIN_BEFORE_BUILD
  dimension: 1024
  normalize: true
  model_path: /models/bge-m3

reranker:
  model: BAAI/bge-reranker-v2-m3
  revision: RESOLVE_AND_PIN_BEFORE_BUILD
  model_path: /models/bge-reranker-v2-m3

chunking:
  version: paragraph-v1
  target_tokens: 800
  max_tokens: 1000
  overlap_tokens: 80

api:
  internal_port: 8000
  auth_profile: private_token
  token_hashes_file: /run/secrets/api-token-hashes
  max_concurrent_inference: 2
  default_response_chars: 30000
```

An operator configuration supplies write/index credentials separately. Expose nonsecret diagnostics as a configuration fingerprint and capabilities, not the entire configuration file.

Dataset downloads are outside this runbook. Model weights are a separate prerequisite: fetch and pin them once if not already available, then run serving and ingestion with local model files. Provide an offline mode that fails clearly on missing files rather than making an unexpected network download.

## 4. Tailscale setup

Reuse the existing tailnet and node where practical. First check whether Tailscale runs on Windows, in WSL, or elsewhere, and inspect the current Serve configuration. Choose an unused route/port without replacing an existing application.

If using the Windows Tailscale node, verify that the Windows side can reach the WSL application through the selected localhost-forwarding arrangement. If using a WSL node, proxy directly to the loopback application endpoint in that distro. Do not depend on a transient WSL IP address when a stable proxy arrangement is available.

Publish the application through Tailscale Serve with HTTPS and restrict access to the intended users/devices. Serve endpoints are accessible inside the tailnet; this is the initial deployment model. [Tailscale Serve documentation](https://tailscale.com/docs/features/tailscale-serve)

Expose the application origin, including `/mcp` and `/api/v1`, through that proxy. Verify from a second authorized device: health, authenticated corpus info, one search, and a document read. Verify that an unauthorized client is rejected. Keep OpenSearch and model-worker endpoints internal.

For clients that support only local stdio MCP, install the lightweight bridge on the client machine and point it to the tailnet HTTPS application URL. For cloud-hosted connectors that cannot join or reach the tailnet, use the separate authenticated public/gateway deployment profile described in the MCP contract. Do not assume a hostname change alone solves reachability.

## 5. Build and first-run procedure

The following commands define the CLI experience to implement. They are examples of the intended operator workflow; they are not commands available in this plan directory today. Every stage writes a machine-readable report and an understandable summary.

```text
court-search doctor --config config/local.yaml
court-search sources inspect --config config/local.yaml
court-search corpus select --name pilot --limit 200000 --strategy stratified
court-search ingest --selection pilot --resume
court-search index lexical --selection pilot --generation pilot-v1 --resume
court-search chunk --selection pilot --resume
court-search embed --selection pilot --resume
court-search index passages --selection pilot --generation pilot-v1 --resume
court-search corpus validate --generation pilot-v1
court-search evaluate --generation pilot-v1 --suite eval/pilot
court-search corpus activate --generation pilot-v1
court-search smoke-test --api-url https://court-host.your-tailnet.ts.net
```

`doctor` checks resources, mounted files, OpenSearch/plugin health, certificates, model pins, GPU availability when requested, disk headroom, and schema compatibility. It does not change host-wide settings automatically. `select` writes a stable selection manifest. Search/evaluation commands must be able to target a ready staging generation before activation.

Launch serving services after dependencies pass health checks; do not rely on container startup order alone. The API can start before a corpus is active, but readiness must say `CORPUS_NOT_READY` until activation. Lexical service can become ready without the model worker when that capability is explicitly configured; corpus info must advertise that state.

After the pilot, use the same CLI stages with a complete selected jurisdiction and appropriately sized partitions. Run the capacity forecast before expanding. Semantic coverage can grow in later generations without rebuilding compatible unchanged lexical partitions.

## 6. Refresh and rollback procedure

1. Add the new local snapshot as a separately identified source; retain the previous manifest.
2. Inspect the source scope and revision, then run the delta report. Do not interpret missing files as deletions from the registry.
3. Compute the plan: new/changed/removed records within authoritative scope, affected partitions, reusable embeddings, estimated time, and required free disk.
4. Build a new generation or changed partitions with the previous generation still serving.
5. Validate counts, filters, citation spans, vector specification, and the fixed evaluation sample. Read actual example records from the staging indexes.
6. Activate the completed manifest in one transaction. Cache keys and new requests use the new generation; existing bounded reads/cursors retain the old one.
7. Check the live smoke-test suite. If it fails, point the active generation back to the previous valid manifest and investigate.
8. Retire old generations only after the retention interval, active cursor lifetime, and snapshot requirements are satisfied. Delete a shared partition only if no retained generation references it.

This workflow runs when a new snapshot is intentionally supplied. A scheduled polling/downloading service is unnecessary for the initial product. A future freshness check may report available revisions without importing them automatically.

## 7. Backups, availability, and observability

Back up source manifests, normalization/chunking specifications, dictionaries, configuration pins, and the SQLite state through a consistent backup operation. Take OpenSearch snapshots of the exact physical indexes referenced by the backed-up active manifest. Snapshot to another disk/host when recovery from local SSD failure is required. A second copy on the same SSD does not provide that protection.

Keep originals or a reproducible source copy and consider backing up embedding caches, since regeneration can be expensive. Snapshot success is insufficient by itself: restore a pilot generation into a clean instance and verify corpus info, sample documents, exact excerpts, and search. Do not copy live OpenSearch data directories as a substitute for its snapshot mechanism.

Log request IDs, effective modes, generation IDs, stage timings, error codes, import progress, and memory/disk measurements. Avoid logging raw user queries or decision text by default. Emit structured logs and a small metrics endpoint; a separate monitoring stack can wait until needed.

`/health/live` indicates that the process is running. `/health/ready` reports whether the configured minimum search capability and an active generation are available, returning only minimal information without authentication. Authenticated diagnostics can include embedding/reranker readiness, queue size, index health, quarantine counts, and free space.

For a personal machine, uptime also depends on Windows sleep, updates, and whether WSL/Docker starts after reboot. Provide a tested startup procedure using the chosen engine and Windows/WSL service arrangement. A container restart policy alone cannot start a stopped WSL environment. Initial availability can reasonably follow the machine's uptime, provided clients receive a clear connection error when it is offline.

## 8. Move the installation to another host

Install the tested container runtime and select CPU or CUDA mode for the destination. Transfer the application release, configuration pins, retained corpus manifests, consistent SQLite backup, and compatible OpenSearch snapshots. Transfer model weights and embedding caches as needed. Restore and validate the same generation before switching the client URL.

When an OpenSearch version upgrade is required, follow its supported upgrade/restore path and test it separately from the host move. A versioned snapshot is not a promise that arbitrary newer/older engines can read it. Raw and normalized Parquet plus pinned model specifications remain the rebuild path.

On Linux/WSL with NVIDIA, use the CUDA worker when supported. On a host without that GPU stack, use CPU inference or configure a reachable model worker running the identical embedding specification. Publish/test architecture-specific images before claiming support for a new architecture.

The MCP client configuration changes only in its backend URL/credentials or local bridge path. The selected LLM, tool schemas, source identifiers, and corpus generation semantics remain independent of the host location.

## 9. Operational completion criteria

The installation is reviewable when a second device can search through MCP, read cited text through the same corpus version, and use the equivalent REST endpoints. Record the installed scope and quality/capacity measurements. Demonstrate interrupted-job recovery, one new-generation activation, rollback, and snapshot restore. Provide the pinned release/configuration, tested client instructions, and a clear record of which data is lexically and semantically indexed.
