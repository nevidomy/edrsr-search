---
language:
- uk
license: cc-by-4.0
pretty_name: "EDRSR passages with BGE-M3 embeddings"
size_categories:
- 100M<n<1B
task_categories:
- feature-extraction
- sentence-similarity
- text-retrieval
tags:
- legal
- legal-nlp
- ukraine
- ukrainian
- court-decisions
- edrsr
- embeddings
- bge-m3
- retrieval
configs:
- config_name: sample
  default: true
  data_files: data/sample/*.parquet
- config_name: civil
  data_files: data/civil/*.parquet
- config_name: administrative
  data_files: data/administrative/*.parquet
- config_name: criminal
  data_files: data/criminal/*.parquet
- config_name: commercial
  data_files: data/commercial/*.parquet
- config_name: administrative_offense
  data_files: data/administrative_offense/*.parquet
---

# EDRSR passages with BGE-M3 embeddings

Ukrainian court decisions from the Unified State Register of Court Decisions
(ЄДРСР / EDRSR, <https://reyestr.court.gov.ua>), split into passages and embedded with
[BAAI/bge-m3](https://huggingface.co/BAAI/bge-m3) (dense, 1024 dimensions). That is
**{{TOTAL_PASSAGES}} passages** with their text, position in the decision, and vector, ready for
semantic search, RAG, clustering, or classification without spending GPU time.

A search service built on these vectors (OpenSearch hybrid search, REST API, and an MCP
server for Claude and other assistants), with setup instructions, lives at
**{{CODE_URL}}**.

## Contents

| config | source files | decisions | passages |
|---|---:|---:|---:|
{{STATS_TABLE}}

`sample` has 2,000 passages from each section for a quick look.

## Fields

| field | type | description |
|---|---|---|
| `passage_id` | string | stable id: `doc_id:text_hash:chunking_version:ordinal` |
| `doc_id` | string | EDRSR document id (join key to the source datasets) |
| `text_hash` | string | SHA-256 of the full decision text the passage was cut from |
| `ordinal` | int | passage number within the decision, from 0 |
| `char_start`, `char_end` | int | character offsets of the passage in the decision text |
| `text` | string | passage text |
| `token_count` | int | estimated tokens (characters / 3) |
| `jurisdiction` | string | `civil`, `administrative`, `criminal`, `commercial`, `administrative_offense` |
| `judgment_code` | string | EDRSR judgment type code |
| `vector` | float32[1024] | L2-normalised BGE-M3 dense embedding (cosine = dot product) |

## How it was made

- **Source:** the `overthelex/edrsr-*` datasets (CC-BY-4.0), revisions of 2026-06-17:
  [edrsr-tspk](https://huggingface.co/datasets/overthelex/edrsr-tspk) (civil, `c0df790`),
  [edrsr-kas](https://huggingface.co/datasets/overthelex/edrsr-kas) (administrative, `69861a4`),
  [edrsr-kpk](https://huggingface.co/datasets/overthelex/edrsr-kpk) (criminal, `482640c`),
  [edrsr-gpk](https://huggingface.co/datasets/overthelex/edrsr-gpk) (commercial, `42e5b4d`),
  [edrsr-kupap](https://huggingface.co/datasets/overthelex/edrsr-kupap) (administrative offences, `cab1133`).
  Texts are pseudonymised at the source registry, as Ukrainian law requires.
- **Passages (`para-pack-v2`):** paragraphs are packed up to ~800 estimated tokens; nothing
  exceeds 1,000; paragraphs longer than that are split with ~80 tokens of overlap.
- **Model:** `BAAI/bge-m3` at revision `5617a9f61b028005a4858fdac845db406aefb181`,
  sentence-transformers, `max_seq_length=1000`, fp16 inference, normalised. Vectors are
  stored as float32 (upcast from fp16; it compresses to the same size).
- **Query side:** embed queries with the same model and revision, normalised, no prefix.

## Usage

```python
from datasets import load_dataset

ds = load_dataset("{{REPO}}", "criminal", split="train", streaming=True)
row = next(iter(ds))
print(row["doc_id"], row["text"][:200], len(row["vector"]))
```

Similarity search with DuckDB straight from the Hub:

```sql
INSTALL httpfs; LOAD httpfs;
SELECT doc_id, left(text, 120) AS snippet,
       array_cosine_similarity(vector::FLOAT[1024], $query::FLOAT[1024]) AS score
FROM 'hf://datasets/{{REPO}}/data/commercial/*.parquet'
ORDER BY score DESC LIMIT 10;
```

Full decision texts and metadata (court, judge, dates, case number) are in the source
datasets; join on `doc_id`.

## License and attribution

CC-BY-4.0, inherited from the source datasets. Please credit the EDRSR register
(reyestr.court.gov.ua), the `overthelex/edrsr-*` datasets, and this dataset. The embedding
model BAAI/bge-m3 is MIT-licensed.

## Limitations

Court decisions as published by the register: pseudonymisation is the register's, and
the texts can contain errors. Retrieval ranks text similarity, not legal relevance or
whether a decision is still in force. Not legal advice.
