from __future__ import annotations

import time
from typing import Any, Iterable
from urllib.parse import urlparse

from opensearchpy import OpenSearch, helpers

from court_search.config import SearchTuning
from court_search.domain.models import DecisionRecord
from court_search.indexing.mappings import (
    decision_chunk_index_name,
    decision_index_body,
    decision_index_name,
    passage_index_body,
    passage_index_name,
)
from court_search.indexing.records import record_from_source, source_from_record


def build_client(tuning: SearchTuning) -> OpenSearch:
    parsed = urlparse(tuning.url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 9200)
    return OpenSearch(
        hosts=[{"host": host, "port": port}],
        http_compress=True,
        use_ssl=parsed.scheme == "https",
        verify_certs=False,
        ssl_show_warn=False,
        timeout=tuning.request_timeout_seconds,
    )


class OpenSearchIndex:
    def __init__(self, tuning: SearchTuning) -> None:
        self.tuning = tuning
        self.client = build_client(tuning)

    def ping(self) -> bool:
        return bool(self.client.ping())

    def info(self) -> dict[str, Any]:
        return dict(self.client.info())

    def plugins(self) -> list[str]:
        return [row["component"] for row in self.client.cat.plugins(format="json")]

    def analyze(self, analyzer: str, text: str, index: str | None = None) -> list[str]:
        body: dict[str, Any] = {"analyzer": analyzer, "text": text}
        if index:
            result = self.client.indices.analyze(index=index, body=body)
        else:
            result = self.client.indices.analyze(body=body)
        return [token["token"] for token in result.get("tokens", [])]

    def recreate(self, generation: str, *, shards: int = 1) -> str:
        name = decision_index_name(generation)
        if self.client.indices.exists(index=name):
            self.client.indices.delete(index=name)
        self.client.indices.create(index=name, body=decision_index_body(shards=shards))
        return name

    def ensure(self, generation: str, *, shards: int = 4) -> str:
        name = decision_index_name(generation)
        if not self.client.indices.exists(index=name):
            self.client.indices.create(index=name, body=decision_index_body(shards=shards))
        return name

    def ensure_chunk(self, generation: str, chunk: int, *, shards: int = 2) -> str:
        name = decision_chunk_index_name(generation, chunk)
        if not self.client.indices.exists(index=name):
            self.client.indices.create(index=name, body=decision_index_body(shards=shards))
        return name

    def delete_index(self, index_name: str) -> None:
        if self.client.indices.exists(index=index_name):
            self.client.indices.delete(index=index_name)

    def index_store_bytes(self, index_name: str) -> int:
        if not self.client.indices.exists(index=index_name):
            return 0
        rows = self.client.cat.indices(index=index_name, format="json", bytes="b")
        if not rows:
            return 0
        return int(rows[0].get("store.size") or 0)

    def ensure_snapshot_repo(self, repo: str, location: str) -> None:
        self.client.snapshot.create_repository(
            repository=repo,
            body={"type": "fs", "settings": {"location": location, "compress": True}},
            verify=True,
        )

    def snapshot_index(self, repo: str, snap: str, index_name: str) -> dict[str, Any]:
        self.client.snapshot.create(
            repository=repo,
            snapshot=snap,
            body={"indices": index_name, "include_global_state": False, "partial": False},
            wait_for_completion=False,
        )
        return self._wait_snapshot(repo, snap)

    def restore_snapshot(self, repo: str, snap: str, index_name: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"include_global_state": False}
        if index_name:
            body["indices"] = index_name
        self.client.snapshot.restore(
            repository=repo,
            snapshot=snap,
            body=body,
            wait_for_completion=False,
        )
        return self._wait_restore(repo, snap, index_name)

    def snapshot_exists(self, repo: str, snap: str) -> bool:
        try:
            result = self.client.snapshot.get(repository=repo, snapshot=snap)
        except Exception:  # noqa: BLE001
            return False
        return bool(result.get("snapshots"))

    def point_alias(self, alias: str, indices: list[str]) -> None:
        actions: list[dict[str, Any]] = []
        if self.client.indices.exists_alias(name=alias):
            current = self.client.indices.get_alias(name=alias)
            for existing in current:
                actions.append({"remove": {"index": existing, "alias": alias}})
        for index_name in indices:
            actions.append({"add": {"index": index_name, "alias": alias}})
        if actions:
            self.client.indices.update_aliases(body={"actions": actions})

    def _wait_snapshot(self, repo: str, snap: str) -> dict[str, Any]:
        while True:
            result = self.client.snapshot.get(repository=repo, snapshot=snap)
            info = (result.get("snapshots") or [{}])[0]
            state = info.get("state")
            if state == "SUCCESS":
                return info
            if state in {"FAILED", "PARTIAL", "INCOMPATIBLE"}:
                raise RuntimeError(f"snapshot {snap} {state}: {info.get('failures')}")
            time.sleep(10)

    def _wait_restore(self, repo: str, snap: str, index_name: str | None = None) -> dict[str, Any]:
        while True:
            if index_name and self.client.indices.exists(index=index_name):
                recovery = self.client.cat.recovery(index=index_name, format="json")
                active = [row for row in recovery if row.get("stage") not in {"done", None}]
                health = self.client.cluster.health(index=index_name)
                pending = int(health.get("unassigned_shards") or 0) + int(
                    health.get("initializing_shards") or 0
                )
                if not active and pending == 0 and health.get("status") in {"green", "yellow"}:
                    count = int(self.client.count(index=index_name).get("count") or 0)
                    return {"snapshot": snap, "index": index_name, "docs": count}
            elif not index_name:
                recovery = self.client.cat.recovery(format="json")
                active = [
                    row
                    for row in recovery
                    if row.get("type") == "snapshot" and row.get("stage") not in {"done", None}
                ]
                if not active and self.snapshot_exists(repo, snap):
                    return {"snapshot": snap, "restored": True}
            time.sleep(10)

    def refresh(self, generation: str) -> None:
        self.client.indices.refresh(index=decision_index_name(generation))

    def finalize(self, generation: str) -> None:
        self.finalize_index(decision_index_name(generation))

    def finalize_index(self, index_name: str) -> None:
        self.client.indices.put_settings(
            index=index_name,
            body={"index": {"refresh_interval": "1s", "translog.durability": "request"}},
        )
        self.client.indices.refresh(index=index_name)

    def bulk_index(
        self,
        generation: str,
        records: Iterable[DecisionRecord],
        *,
        finalize: bool = True,
        chunk_size: int = 200,
        thread_count: int = 1,
        index_name: str | None = None,
    ) -> dict[str, int]:
        name = index_name or decision_index_name(generation)
        actions = (
            {
                "_op_type": "index",
                "_index": name,
                "_id": record.doc_id,
                "_source": source_from_record(record),
            }
            for record in records
        )
        success = 0
        errors = 0
        if thread_count > 1:
            for ok, _item in helpers.parallel_bulk(
                self.client,
                actions,
                raise_on_error=False,
                request_timeout=180,
                chunk_size=chunk_size,
                thread_count=thread_count,
            ):
                if ok:
                    success += 1
                else:
                    errors += 1
        else:
            indexed, failed = helpers.bulk(
                self.client,
                actions,
                raise_on_error=False,
                request_timeout=180,
                chunk_size=chunk_size,
            )
            success = int(indexed)
            errors = len(failed) if isinstance(failed, list) else int(failed)
        if finalize:
            self.finalize(generation)
        return {"indexed": int(success), "errors": int(errors), "index": name}

    def ensure_passage_index(self, spec_name: str = "bge-m3-dense-v1", *, recreate: bool = False, shards: int = 2) -> str:
        name = passage_index_name(spec_name)
        if recreate and self.client.indices.exists(index=name):
            self.client.indices.delete(index=name)
        if not self.client.indices.exists(index=name):
            self.client.indices.create(index=name, body=passage_index_body(shards=shards))
        return name

    def passage_count(self, spec_name: str = "bge-m3-dense-v1") -> int:
        name = passage_index_name(spec_name)
        if not self.client.indices.exists(index=name):
            return 0
        return int(self.client.count(index=name).get("count") or 0)

    def bulk_passages(self, index_name: str, actions: Iterable[dict[str, Any]]) -> tuple[int, int]:
        success = 0
        errors = 0
        for ok, _item in helpers.streaming_bulk(
            self.client,
            ({**action, "_index": index_name} for action in actions),
            raise_on_error=False,
            request_timeout=600,  # merges on a ~1 TB index can stall a bulk for minutes
            chunk_size=200,
            # Parallel loaders can fill the write queue: back off and retry 429s
            # instead of silently counting those passages as failed.
            max_retries=8,
            initial_backoff=2,
            max_backoff=120,
        ):
            if ok:
                success += 1
            else:
                errors += 1
        return success, errors

    def knn_passages(
        self,
        vector: list[float],
        *,
        spec_name: str = "bge-m3-dense-v1",
        k: int = 50,
        jurisdictions: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        name = passage_index_name(spec_name)
        if not self.client.indices.exists(index=name):
            return []
        knn: dict[str, Any] = {"vector": {"vector": vector, "k": k}}
        if jurisdictions:
            knn["vector"]["filter"] = {"terms": {"jurisdiction": jurisdictions}}
        result = self.client.search(
            index=name,
            body={"size": k, "query": {"knn": knn}, "_source": True},
            request_timeout=30,
        )
        hits = []
        for hit in result.get("hits", {}).get("hits") or []:
            src = hit.get("_source") or {}
            src["score"] = float(hit.get("_score") or 0.0)
            hits.append(src)
        return hits

    def get(self, generation: str, doc_id: str) -> DecisionRecord | None:
        name = decision_index_name(generation)
        # GET-by-id is a single-index op; the full-v1 alias spans many chunks.
        try:
            result = self.client.search(
                index=name,
                body={"query": {"ids": {"values": [doc_id]}}, "size": 1},
            )
        except Exception:  # noqa: BLE001
            return None
        hits = result.get("hits", {}).get("hits") or []
        if not hits:
            return None
        src = hits[0].get("_source")
        if not src:
            return None
        src.setdefault("doc_id", hits[0].get("_id") or doc_id)
        return record_from_source(src)

    def count(self, generation: str) -> int:
        name = decision_index_name(generation)
        if not self.client.indices.exists(index=name):
            return 0
        result = self.client.count(index=name)
        return int(result.get("count") or 0)

    def list_by_case(
        self,
        generation: str,
        case_number: str,
        *,
        court_codes: list[str] | None,
        jurisdictions: list[str] | None,
        limit: int,
    ) -> list[DecisionRecord]:
        name = decision_index_name(generation)
        filters: dict[str, Any] = {"case_number": case_number}
        if court_codes:
            filters["court_codes"] = court_codes
        if jurisdictions:
            filters["jurisdictions"] = jurisdictions
        body = {
            "size": limit,
            "query": {"bool": {"filter": _filter_clauses(filters)}},
            "sort": [
                {"decision_date": {"order": "asc", "missing": "_last"}},
                {"doc_id": {"order": "asc"}},
            ],
        }
        result = self.client.search(index=name, body=body)
        records: list[DecisionRecord] = []
        for hit in result.get("hits", {}).get("hits", []):
            src = hit.get("_source")
            if src:
                records.append(record_from_source(src))
        return records

    def stats(self, generation: str) -> dict[str, Any]:
        name = decision_index_name(generation)
        if not self.client.indices.exists(index=name):
            return {"count": 0, "by_jurisdiction": {}, "min_date": None, "max_date": None}
        body = {
            "size": 0,
            "aggs": {
                "by_jurisdiction": {"terms": {"field": "jurisdiction", "size": 20}},
                "dates": {"stats": {"field": "decision_date"}},
            },
        }
        result = self.client.search(index=name, body=body)
        aggs = result.get("aggregations") or {}
        by_jurisdiction = {
            bucket["key"]: int(bucket["doc_count"])
            for bucket in aggs.get("by_jurisdiction", {}).get("buckets", [])
        }
        dates = aggs.get("dates") or {}
        return {
            "count": self.count(generation),
            "by_jurisdiction": by_jurisdiction,
            "min_date": dates.get("min_as_string"),
            "max_date": dates.get("max_as_string"),
        }

    def search(
        self,
        generation: str,
        query: str,
        *,
        filters: dict[str, Any],
        limit: int,
    ) -> list[tuple[str, float]]:
        name = decision_index_name(generation)
        must: list[dict[str, Any]] = [
            {
                "multi_match": {
                    "query": query,
                    "fields": ["text^1.0", "text.plain^1.2"],
                    "type": "best_fields",
                    "operator": "and",
                }
            }
        ]
        filter_clauses = _filter_clauses(filters)
        for phrase in filters.get("required_phrases") or []:
            must.append({"match_phrase": {"text.plain": phrase}})
        body = {
            "size": limit,
            "query": {"bool": {"must": must, "filter": filter_clauses}},
            "_source": ["doc_id"],
        }
        result = self.client.search(index=name, body=body)
        hits = []
        for hit in result.get("hits", {}).get("hits", []):
            hits.append((str(hit["_id"]), float(hit.get("_score") or 0.0)))
        return hits

    def search_records(
        self,
        generation: str,
        query: str,
        *,
        filters: dict[str, Any],
        limit: int,
    ) -> list[tuple[DecisionRecord, float]]:
        name = decision_index_name(generation)
        must: list[dict[str, Any]] = [
            {
                "multi_match": {
                    "query": query,
                    "fields": ["text^1.0", "text.plain^1.2"],
                    "type": "best_fields",
                    "operator": "and",
                }
            }
        ]
        filter_clauses = _filter_clauses(filters)
        for phrase in filters.get("required_phrases") or []:
            must.append({"match_phrase": {"text.plain": phrase}})
        body = {
            "size": limit,
            "query": {"bool": {"must": must, "filter": filter_clauses}},
        }
        result = self.client.search(index=name, body=body)
        hits: list[tuple[DecisionRecord, float]] = []
        for hit in result.get("hits", {}).get("hits", []):
            src = hit.get("_source")
            if not src:
                continue
            src.setdefault("doc_id", hit.get("_id"))
            hits.append((record_from_source(src), float(hit.get("_score") or 0.0)))
        return hits


def _filter_clauses(filters: dict[str, Any]) -> list[dict[str, Any]]:
    clauses: list[dict[str, Any]] = []
    if values := filters.get("jurisdictions"):
        clauses.append({"terms": {"jurisdiction": values}})
    if values := filters.get("court_codes"):
        clauses.append({"terms": {"court_code": values}})
    if values := filters.get("judgment_codes"):
        clauses.append({"terms": {"judgment_code": values}})
    if values := filters.get("category_codes"):
        clauses.append({"terms": {"category_code": values}})
    if value := filters.get("case_number"):
        clauses.append({"term": {"case_number": value}})
    date_range: dict[str, str] = {}
    if value := filters.get("date_from"):
        date_range["gte"] = value
    if value := filters.get("date_to"):
        date_range["lte"] = value
    if date_range:
        clauses.append({"range": {"decision_date": date_range}})
    return clauses
