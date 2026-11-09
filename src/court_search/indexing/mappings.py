from __future__ import annotations

from typing import Any


def decision_index_body(*, shards: int = 1) -> dict[str, Any]:
    return {
        "settings": {
            "number_of_shards": shards,
            "number_of_replicas": 0,
            "refresh_interval": "-1",
            "translog.durability": "async",
            "translog.flush_threshold_size": "1gb",
            "analysis": {
                "analyzer": {
                    "ukrainian_plain": {
                        "type": "custom",
                        "tokenizer": "standard",
                        "filter": ["lowercase"],
                    }
                }
            },
        },
        "mappings": {
            "dynamic": "strict",
            "properties": {
                "doc_id": {"type": "keyword"},
                "document_version": {"type": "keyword"},
                "case_number": {"type": "keyword"},
                "case_number_raw": {"type": "keyword"},
                "court_code": {"type": "keyword"},
                "jurisdiction": {"type": "keyword"},
                "judgment_code": {"type": "keyword"},
                "category_code": {"type": "keyword"},
                "decision_date": {"type": "date", "format": "strict_date||yyyy-MM-dd"},
                "decision_date_raw": {"type": "keyword"},
                "source_year": {"type": "integer"},
                "decision_year": {"type": "integer"},
                "justice_kind": {"type": "integer"},
                "justice_kind_name": {"type": "keyword"},
                "judge": {"type": "keyword"},
                "text_hash": {"type": "keyword"},
                "registry_url": {"type": "keyword"},
                "source_refs_json": {"type": "keyword", "index": False, "doc_values": False},
                "text": {
                    "type": "text",
                    "analyzer": "ukrainian",
                    "fields": {"plain": {"type": "text", "analyzer": "ukrainian_plain"}},
                },
            },
        },
    }


def passage_index_name(spec_name: str = "bge-m3-dense-v1") -> str:
    safe = spec_name.lower().replace("_", "-")
    return f"passages-{safe}"


def passage_index_body(*, shards: int = 2) -> dict[str, Any]:
    return {
        "settings": {
            "index": {
                "knn": True,
                "number_of_shards": shards,
                "number_of_replicas": 0,
                "refresh_interval": "5s",
            }
        },
        "mappings": {
            "dynamic": "strict",
            "properties": {
                "passage_id": {"type": "keyword"},
                "doc_id": {"type": "keyword"},
                "jurisdiction": {"type": "keyword"},
                "section": {"type": "keyword"},
                "char_start": {"type": "integer"},
                "char_end": {"type": "integer"},
                "vector": {
                    "type": "knn_vector",
                    "dimension": 1024,
                    "method": {
                        "name": "hnsw",
                        "engine": "lucene",
                        "space_type": "cosinesimil",
                        "parameters": {"ef_construction": 128, "m": 16},
                    },
                },
            },
        },
    }


def decision_index_name(generation: str) -> str:
    safe = generation.lower().replace("_", "-")
    return f"decisions-{safe}-0"


def decision_chunk_index_name(generation: str, chunk: int) -> str:
    safe = generation.lower().replace("_", "-")
    return f"decisions-{safe}-c{chunk:03d}"


def snapshot_id(chunk: int) -> str:
    return f"chunk-{chunk:03d}"


def should_roll_chunk(store_bytes: int, limit_bytes: int) -> bool:
    return limit_bytes > 0 and store_bytes >= limit_bytes
