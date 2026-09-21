# Ukrainian court search — implementation plan

Build specification · 20 September 2026

This plan starts with the Hugging Face Parquet files already downloaded. It specifies a searchable, versioned collection of judicial decisions, a reusable HTTP API, and an MCP interface that different LLM applications can call. It is a specification for the implementation; the commands and application components described here still need to be built.

Read this document for architecture, indexing, and the delivery sequence. Companion documents define the [API and MCP contracts](API_AND_MCP.md) and the [deployment and operating procedure](DEPLOYMENT.md).

## 1. Decisions and scope

**Recommended stack: OpenSearch + local Parquet + SQLite + Python/FastAPI + the official MCP Python SDK.** Run the services through Docker Compose inside WSL2, and expose the application through the existing Tailscale setup. Use the NVIDIA GPU for embeddings and reranking, with a CPU mode available on other machines.

The working hardware assumption is 64 GB system RAM, ample SSD space, and the NVIDIA GPU described in the conversation. Measure actual dedicated VRAM before selecting inference batch sizes. Initial traffic is approximately 100 requests per day, with a few users and occasional data refreshes.

| Component | Choice | Responsibility |
|---|---|---|
| Original data | Existing local Parquet files | Immutable input and source provenance |
| Import and inspection | Python, PyArrow; DuckDB for bounded offline audits | Stream records, validate, partition, and produce manifests |
| Search database | OpenSearch, single node initially | BM25, exact metadata filters, passage vectors, document reads |
| Application state | SQLite in WAL mode | Jobs, checkpoints, corpus generations, configuration fingerprints |
| Embeddings | `BAAI/bge-m3`, dense output | Encode questions and decision passages |
| Reranking | `BAAI/bge-reranker-v2-m3` | Rescore a small set of candidate passages |
| Service | Python, FastAPI, Pydantic | Shared search logic, REST contracts, health and diagnostics |
| LLM connection | Official MCP Python SDK | Streamable HTTP endpoint and a small stdio bridge |
| Hosting | Docker Compose, Tailscale Serve | Reproducible deployment and private access |

OpenSearch keeps lexical and vector retrieval in one search engine. SQLite stores small operational records, not tens of millions of full texts or hundreds of millions of embeddings. Keep bulk derived data in partitioned files. PostgreSQL becomes useful if the product later gains substantial multi-user state, annotation workflows, or concurrent writers; it is unnecessary for the initial workload.

The MVP supports natural-language search, exact case/document lookup, court/date/type filters, evidence passages, complete document reading in bounded pages, and chronological lists of available decisions sharing a case number. It does not infer whether a decision remains legally effective, reconstruct a complete procedural history, or claim that all case materials are present.

A retrieved record represents a judicial act. Several acts can belong to one case. The corpus is a downloaded snapshot, and its presence or latest observed decision date does not establish completeness of the live registry.

## 2. Architecture and boundaries

```text
OFFLINE / OCCASIONAL
Local HF Parquet (read only)
    -> inventory + schema audit
    -> validate / deduplicate / preserve provenance
    -> normalized decision partitions + paragraph offsets
    -> chunk partitions -> embedding worker -> embedding cache
    -> OpenSearch staging indexes -> quality checks -> activate generation
                         |
                  SQLite job ledger

ONLINE
LLM application
    |-- MCP Streamable HTTP ------------------|
    |-- MCP stdio bridge -> REST -------------|-> SearchService
    |-- ordinary tool-calling adapter -> REST-|     |-- OpenSearch
                                                  |-- model worker
                                                  |-- active corpus manifest

SearchService -> ranked decisions + exact excerpts + source/version IDs
LLM application -> explanation and citations for the user
```

The service does not require a generative LLM. Changing the user's chat model does not require reindexing. Changing the embedding model does require a compatible vector index; query and passage embeddings must share the exact embedding specification.

REST and MCP call the same typed service methods. Put ranking, filtering, pagination, and evidence assembly in the domain service, never separately inside each transport. The stdio bridge is a small client of the existing backend; starting another LLM application must not start another search database or load another embedding model.

The model worker exposes internal embedding and reranking operations. It can run on CPU, on the local GPU, or on another explicitly configured host. It is not exposed as an unrestricted public inference service. Keep its interface replaceable so the retrieval pipeline is independent of the chosen inference library.

## 3. Import the downloaded Hugging Face data

### Source inventory

Support configured local roots for `overthelex/edrsr-tspk`, `edrsr-kas`, `edrsr-kpk`, `edrsr-gpk`, and `edrsr-kupap`. A root may be a normal download directory or a selected local Hugging Face snapshot. Do not scan multiple HF revisions as though they were additional data. The [dataset repository](https://huggingface.co/datasets/overthelex/edrsr-tspk) is the provenance reference; the local files determine what this installation actually contains.

For each input file record its source repository, relative path, byte size, SHA-256, Parquet schema, row count, row-group count, and known HF revision. Record an unknown revision as unknown. Resolve duplicate paths/symlinks and identical files before ingesting records. Inventory first; source cards are not an authoritative count of the downloaded subset.

Expected fields to validate against the actual files are `doc_id`, `cause_num`, `court_code`, `judge`, `adjudication_date`, `judgment_code`, `category_code`, `year`, `justice_kind`, `justice_kind_name`, and `text`. Handle optional/extra columns without dropping them from provenance. Fail clearly on incompatible required columns.

Sample across files, jurisdictions, years, document lengths, and judgment types. Report empty text, invalid dates, duplicate IDs, conflicting IDs, length percentiles, and missing metadata. The initial pilot should contain about 200,000 representative decisions, selected deterministically, plus deliberately selected edge cases. Preserve its document ID manifest so every ranking experiment uses the same data.

### Canonical decision record

| Field | Representation and handling |
|---|---|
| `doc_id` | String in every application/API contract; preserve numeric identity without JavaScript integer assumptions |
| `case_number_raw` / `case_number` | Original `cause_num` plus conservative whitespace/separator normalization; never discard the original |
| `court_code` | Keyword string, preserving leading zeros |
| `judge_raw` | Original value; do not assume it is a reliable normalized person identity |
| `decision_date_raw` / `decision_date` | Original date plus validated civil date; invalid/missing parses remain null |
| `source_year` / `decision_year` | Supplied year and derived year separately; report disagreements |
| `jurisdiction` | Stable application enum with source code/name retained |
| `judgment_code`, `category_code` | Strings with nullable values; retain source values and dictionary version when labels exist |
| `text` | Exact downloaded text, preserved without whitespace rewriting |
| `text_hash` | SHA-256 of the UTF-8 encoding of `text` |
| `document_version` | Hash of canonical text and meaningful metadata; excludes ingestion timestamps |
| `source_refs` | Repository/revision, file hash, row group, row offset, attribution information |
| `registry_url` | Constructed registry review URL for the ID, explicitly identified as a source link |
| `paragraphs` | Application-assigned paragraph IDs and half-open character spans into `text` |

Use these jurisdiction enums: `civil`, `administrative`, `criminal`, `commercial`, `administrative_offense`. Validate their mapping against source codes and repository membership during the audit. Unknown or contradictory classifications remain visible as issues; do not silently relabel them.

Court and judgment-code dictionaries are separate reference inputs. Their presence in the HF text files must not be assumed. Ship versioned mappings where provenance is available, and otherwise display codes with a null label. Do not generate official-looking labels from an LLM.

### Validation, duplicate handling, and recovery

Read Parquet in bounded record batches; never load the entire corpus into a dataframe. Write normalized partitions, for example by jurisdiction, year, and a bounded ID-hash bucket. Use disk-backed joins/sorts for global duplicate reconciliation instead of keeping all IDs in Python memory. Keep files large enough to avoid millions of tiny files.

For identical document IDs with identical canonical contents, keep one indexed document and merge provenance. Different IDs with similar text remain distinct acts. Conflicting contents under one ID go into a conflict report; select a version only when reliable source chronology or an explicit resolution rule supports that choice. File enumeration order is not a version policy.

Quarantine missing IDs, unusable text, and unresolved record conflicts. Invalid nonessential metadata can remain null on an otherwise searchable record. Persist reason codes and source positions so every rejected record can be inspected and retried.

Checkpoint each stage by input file hash, row group, partition, and transformation version. Write outputs to temporary paths, flush them, then publish a completed manifest entry. Use deterministic OpenSearch document IDs and retry failed bulk items individually. Resume after termination without creating duplicate records or repeating completed model work.

The audit must reconcile every selected input row into an accepted record contribution, a duplicate contribution, or quarantine. Report unique searchable decisions separately from source rows.

## 4. Text, passages, and citations

Use the unchanged downloaded `text` as the citation source. Paragraph and excerpt offsets refer to Unicode code points, with an inclusive start and exclusive end. This is an application convention, not byte offsets or JavaScript UTF-16 offsets. Convert engine/tokenizer offsets to this convention where necessary, including Lucene's UTF-16 offsets, and validate each returned quote against the original source slice. Return the actual excerpt as well, so clients do not have to implement slicing to show a citation.

Search normalization belongs in analyzers or derived model inputs. It must not change the stored citation text. Retain anonymization placeholders. Avoid broad removal of repeated legal wording: repetitions can contain the dispositive statement or the exception that matters to the query.

Initial chunking parameters are a target of 800 embedding-tokenizer tokens, a hard limit of 1,000, and about 80 tokens of overlap. Tune these on the pilot. Prefer paragraph boundaries; optionally detect headings and the dispositive section with conservative rules. Long paragraphs need sentence/token splits with source offsets. Short final sections must not disappear. Do not silently truncate overlong documents or chunks.

A `passage_id` is derived from `doc_id`, `text_hash`, chunking version, and span/ordinal. A citation includes that ID, the document version, source offsets, and exact text. Application paragraph numbers are labeled as such.

Persist an embedding cache in bulk shard files. Its key includes the exact model revision, tokenizer revision, preprocessing specification, and model-input text hash. Keep a partitioned key-to-vector manifest; do not create one filesystem file per vector. Metadata-only corrections should reuse embeddings when the encoded text is unchanged.

Start with the dense 1,024-dimensional output of [BGE-M3](https://huggingface.co/BAAI/bge-m3). It is a multilingual baseline to evaluate on Ukrainian legal questions, not an assumption of best accuracy. Pin the model and tokenizer revisions, pooling, normalization, dimension, and numeric format. Use the same implementation for indexing and query encoding. Evaluate the multilingual [BGE reranker](https://huggingface.co/BAAI/bge-reranker-v2-m3) on the same judgments; budget its paired query/passage input explicitly.

## 5. OpenSearch indexes

Use two index families for each immutable corpus generation:

| Index family | Unit | Contents |
|---|---|---|
| `decisions-<generation>-<partition>` | One decision | Full citation text, metadata, paragraph offsets, BM25 fields, provenance |
| `passages-<generation>-<partition>` | One embedded passage | Vector, document/version IDs, source span, and copied filter metadata |

Store full text once in the decision index. Passage records reference spans in that text; the worker's passage text remains available in offline partitions/cache. Fetch candidate documents in batches with byte/memory budgets when constructing evidence, and send only selected passages to the model worker. Exclude embedding arrays from API responses and avoid a redundant JSON vector copy in stored source where the pinned engine supports doing so safely.

The active corpus manifest lists physical index names. A request resolves one generation at its start and uses those exact indexes throughout the request. This prevents mixed document and passage versions during activation. Aliases may be provided for administration, but are not the transaction boundary for application requests.

### Lexical fields

Map exact fields such as IDs, case numbers, jurisdiction, and codes as `keyword`; validated dates as `date`; and text as analyzed fields. Use `dynamic: strict` for canonical index records so schema drift is handled by ingestion.

Use two text views: a Ukrainian morphological field for recall and a lowercase, unstemmed field without stop-word removal for phrases and distinctions such as negation. OpenSearch's Ukrainian analyzer requires the `analysis-ukrainian` plugin and does not expose its individual filters for arbitrary customization. Therefore the unstemmed field needs its own separate analyzer. [OpenSearch Ukrainian analyzer documentation](https://docs.opensearch.org/latest/analyzers/language-analyzers/ukrainian/)

Build a custom pinned OpenSearch image with the matching plugin. Verify `_analyze` results for inflected Ukrainian words, apostrophe variants, case numbers, legal article references, and negation. Search the morphological and unstemmed fields together with weights selected through evaluation. Preserve raw case-number lookup independently of tokenized text search. Phrase matching means adjacent analyzed tokens; do not describe it as byte-for-byte substring matching.

### Vector fields and scale

Use cosine-compatible normalized embeddings and a supported approximate nearest-neighbor index. Benchmark OpenSearch's `on_disk` mode with the pinned release, including its quantization and full-precision rescoring behavior. It trades memory and storage access against latency/recall; select parameters from measurements, not an assumed compression ratio. [OpenSearch disk-based vector search](https://docs.opensearch.org/latest/vector-search/optimizing-storage/disk-based-vector-search/)

Start the pilot with one primary shard per index family and zero replicas on the single node. Select full-scale partition and shard sizes from measured bytes and latency. Do not create one shard per source file or one giant shard for the entire registry. A single node has no node-failure redundancy; snapshots provide recovery.

Build in bulk with refresh temporarily disabled on staging indexes, bounded byte-sized requests, backpressure, and retries. Restore normal refresh settings before validation and activation. Do not routinely force-merge large live indexes; any staging optimization must justify its temporary disk and I/O cost.

## 6. Query and ranking pipeline

1. Validate query length and typed filters. Identify an explicit document ID or case number conservatively. Use exact lookup for an exact identifier request.
2. Read the active generation and its lexical/semantic coverage. Validate any requested historical generation.
3. Run BM25 over decisions and encode the query for vector search over passages. Apply the same applicable metadata filters inside both searches, not just after their top results return.
4. Retrieve roughly 100 lexical decisions and 300 vector passages initially. Collapse passage hits to a ranked list of unique decisions by each decision's best passage. Retain additional nonoverlapping passages for evidence. Bound adaptive overfetching when one long decision dominates.
5. Fuse the two decision rankings in the application using reciprocal rank fusion: `sum(1 / (60 + rank))` for available branches, with one-based ranks. Keep original branch ranks for diagnostics. Do not add raw BM25 and cosine scores.
6. For the top 50 fused decisions, select at most two promising passages each. Semantic hits already supply candidates. For lexical-only hits, select source spans around matched terms using offsets and bounded local passage scoring. Never rerank only the beginning of every decision.
7. Rerank up to 100 query/passage pairs. Rank decisions by their best passage score, using fused rank as a deterministic tie-breaker. Tune this policy against a fusion-only baseline; reranking is optional if it does not improve measured quality.
8. Return the requested number of decisions, usually 10, with concise exact excerpts, metadata, citations, coverage information, and continuation handles. Avoid repeating overlapping evidence.

The numeric thresholds above are initial settings, not performance guarantees. Ranking scores are retrieval scores, not probabilities or confidence that a legal conclusion is correct.

Required phrases need a separate eligibility step because the passage index does not duplicate the full document text. Apply them inside lexical retrieval; batch-check vector candidate document IDs against the same phrase conditions in the decision index before fusion. Discard ineligible candidates and overfetch within a fixed budget. This preserves the constraint but may reduce semantic recall, so return an indication when the budget is exhausted. Revalidate all hard constraints on the final candidates before producing results.

Keep explicit user filters separate from query rewriting. An LLM host can reformulate a conversational question into several search calls, but should not silently invent a court, date restriction, or outcome filter. Useful expansions should preserve the original request and expose the effective search arguments.

When semantic indexing covers only part of the corpus, continue lexical search over the full active lexical scope. Report semantic coverage separately. This creates uneven semantic recall, which must be visible in both evaluation and user-facing results. Do not imply that a lack of vector hits proves no relevant act exists.

If embedding/reranking is unavailable, a caller that allows fallback receives lexical results and an explicit degradation reason. A caller requesting strict semantic behavior receives a retryable capability error. Dimension/model mismatches never fall back to an incompatible vector query.

## 7. Refresh strategy for infrequent updates

Treat updates as deliberate batch jobs. There is no need for a streaming ingestion platform, continuously running download service, or frequent full re-embedding.

Each generation records input manifests, transformation versions, reference dictionary versions, embedding and reranker specifications, physical index names, counts, date distributions, and lexical/semantic scope. Keep `ingested_at`, known source publication time, observed decision dates, and any authoritative coverage cutoff as separate fields. Leave the cutoff unknown when the source does not state one.

Compare new file/record hashes to the previous manifest. Reuse unchanged normalized records and embedding shards. Build new affected index partitions; reference unchanged immutable partitions from the new generation where compatible. For a changed partition, assemble its complete new contents, including unchanged records, before activating it. Metadata corrections must propagate to passage filter fields as well as decisions.

After validation, update the active-generation pointer in one SQLite transaction. Existing requests and cursors retain their original generation. Keep at least the prior working generation until rollback and retention requirements are satisfied. Shared immutable partitions require reference-aware cleanup.

A partial new HF download is not evidence that absent records were withdrawn. Apply deletion only from an explicitly complete replacement scope or a separate verified withdrawal input. Maintain a small immediate exclusion list for known withdrawn records; check it on reads, searches, and cached/cursor results, and version it in cache keys.

Forecast staging, retained generations, and temporary merge space before a refresh. Reusing embeddings saves computation, but does not eliminate the disk needed to rebuild a changed search partition.

## 8. Capacity and operating targets

Daily request count affects serving concurrency. Corpus size and passage count determine the initial embedding and indexing cost. Measure both independently.

For `D` decisions and mean `P` passages, vector count is `D × P`. At 1,024 dimensions, float32 vectors alone occupy approximately `D × P × 1,024 × 4` bytes before indexes, text, metadata, caches, or backups. Float16 offline caches use half that raw payload; the search engine's on-disk representation is a separate measurement. As illustrations, 10 million decisions averaging five passages produce about 205 GB of float32 vector payload alone; 100 million at the same average produce about 2.05 TB. These are sensitivity examples, not measured sizes of the downloaded collection.

The pilot must report decisions/second, embedding tokens/second, passages/decision distribution, index bytes/decision, bytes/passage, peak RAM/VRAM, and search latency with both cold and warm caches. Estimate full-build time and SSD needs from those measurements, including at least one staging generation for changed partitions and explicit disk headroom.

Initial service objectives on the measured pilot are warm lexical p95 below 3 seconds and warm hybrid-plus-reranking p95 below 10 seconds at two concurrent requests. Treat these as targets to validate, not promised timings on the uninspected host. Capture cold-start latency separately. Set query deadlines and small inference concurrency limits instead of letting requests exhaust GPU memory.

Roll out in this order: representative pilot; full chosen initial jurisdiction; then broader lexical coverage and staged semantic coverage. Keep all five source adapters supported from the start. The capacity report determines whether the final scope fits on this machine or needs another disk/node or a smaller indexed time range.

## 9. Repository structure to implement

```text
court-search/
  pyproject.toml                 # pinned Python dependency definitions
  uv.lock
  src/court_search/
    domain/                     # canonical models, filters, citation contracts
    ingest/                     # local source adapters, validation, checkpoints
    text/                       # source spans, paragraphs, chunking
    models/                     # embedding/reranker provider interfaces
    indexing/                   # mappings, bulk writer, generation builder
    retrieval/                  # exact lookup, BM25, vectors, fusion, evidence
    storage/                    # manifests, SQLite, partition readers
    api/                        # REST endpoints, authentication, OpenAPI
    mcp/                        # tool definitions, HTTP transport, stdio bridge
    cli/                        # inspect, build, validate, activate, restore
  config/                       # example configuration and versioned dictionaries
  deployment/                   # images, Compose profiles, private proxy examples
  contracts/                    # generated JSON Schema and OpenAPI artifacts
  eval/                         # queries, relevance judgments, reports
  tests/                        # meaningful unit, integration, and recovery tests
  docs/                         # install, operation, client setup
```

Keep data, model weights, secrets, and generated indexes outside the repository. The application configuration points to them. Package a lightweight client/bridge installation separately from the GPU inference dependencies.

## 10. Delivery sequence and acceptance gates

| Milestone | Work and deliverable | Acceptance before moving on |
|---|---|---|
| 0. Inventory and pinning | Inspect downloaded files and actual WSL resources; choose tested dependency/image/model revisions; produce a source audit | Actual schema/counts known; discrepancies and unknown coverage recorded; no source files modified |
| 1. Repeatable ingestion | Canonical schema, disk-backed deduplication, quarantine, source spans, resumable jobs | Pilot accounts for every input row; restarting a killed job yields identical record identities and text hashes |
| 2. Useful lexical search | Decision mappings, Ukrainian/plain analyzers, exact lookup, filters, bounded document reading | Known-ID and case lookups pass; filters never leak records outside requested scope; excerpts match source text |
| 3. Semantic retrieval | Chunker, cached embeddings, vector index, decision-level fusion | Query/index specifications match; complete pilot passage coverage; benchmark against BM25 |
| 4. Reranking and evaluation | Passage selection, reranking, diagnostics, quality/capacity report | Demonstrated relevance improvement or documented choice to leave reranking disabled; measured full-scope forecast |
| 5. Stable API and MCP | Five read-only tools, REST equivalents, stdio bridge, Streamable HTTP, generated schemas | Same results through REST and both MCP transports; tested in two independent MCP hosts and a basic function-calling adapter |
| 6. Deployment and recovery | CPU/CUDA Compose profiles, WSL/Tailscale instructions, snapshots, health endpoints | Works from a second authorized device; CPU mode works; service restart and generation rollback demonstrated |
| 7. Controlled scale-up | Build complete initial jurisdiction, then expand approved capacity scope | Search remains usable during imports; coverage is accurate; disk/memory/latency targets checked at each expansion |
| 8. Product polish | Minimal query/results/reader page consuming the same API, saved example searches, operator guide | A user can inspect supporting text and follow a source link without understanding the internal stack |

MVP delivery includes milestones 0–6 on the pilot, an explicit installed coverage manifest, and a capacity-backed expansion decision. Completion of a pilot must not be described as completion of full-corpus indexing. Full selected-scope delivery requires milestone 7 as well.

### Evaluation and correctness requirements

Create approximately 150 evaluation questions spanning exact citations, case numbers, article references, Ukrainian inflections, natural-language factual situations, procedural outcomes, long documents, mixed-language phrasing, and date/court combinations. Include questions with no known relevant result. Use pooled candidates from BM25, semantic search, and hybrid search, with human-reviewed relevance judgments. Hold back a test set; split related cases together if later training models.

Compare BM25, semantic-only, fusion, and fusion-plus-reranking using recall@100 and nDCG@10, with results broken down by query type and indexed scope. Avoid assigning a universal quality threshold before establishing relevance judgments. Promote changes when they improve the chosen aggregate measure without unacceptable regressions in exact lookup or filter correctness.

Required correctness checks include exact source-excerpt matching, metadata filter enforcement in both retrieval branches, stable document reads across refreshes, explicit partial coverage, malformed input handling, bounded outputs, duplicate/conflict accounting, and recovery after an interrupted bulk write. Check that document text containing instructions is returned as source content, never executed or treated as a tool command.

Integration checks must cover one genuine OpenSearch instance with the actual Ukrainian plugin, pinned vector settings, both inference profiles where hardware exists, MCP schemas/transports, private remote access, and backup restoration. Small mocked tests alone do not validate this system.

## 11. Later improvements, driven by evidence

Add legal citation extraction and case relationships only after stable document IDs and provenance are reliable. Add embeddings fine-tuned for Ukrainian legal retrieval only after collecting suitable relevance pairs. Add summaries as derived, versioned content with links to source spans, keeping them distinct from the original act. Consider a larger search deployment or different vector engine only when the measured capacity/latency report identifies a concrete limitation.

The first useful product is a reliable search-and-read service with traceable evidence and portable tools. Its quality should be established before adding autonomous legal research workflows.
