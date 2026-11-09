from __future__ import annotations

import json
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

import pyarrow.parquet as pq

from court_search.config import AppConfig, SourceConfig
from court_search.domain.enums import Jurisdiction
from court_search.domain.models import DecisionRecord, SourceRef
from court_search.indexing.mappings import (
    decision_chunk_index_name,
    decision_index_name,
    should_roll_chunk,
)
from court_search.indexing.opensearch import OpenSearchIndex
from court_search.ingest.canonical import REQUIRED_COLUMNS, build_record
from court_search.ingest.pipeline import _jurisdiction_from_repo
from court_search.ingest.progress import ProgressLog
from court_search.storage.db import StateStore

DEFAULT_CHUNK_GIB = 80
ALL_JURISDICTIONS = [
    Jurisdiction.CIVIL,
    Jurisdiction.ADMINISTRATIVE,
    Jurisdiction.CRIMINAL,
    Jurisdiction.COMMERCIAL,
    Jurisdiction.ADMINISTRATIVE_OFFENSE,
]


def ingest_full(
    config: AppConfig,
    store: StateStore,
    *,
    name: str,
    jurisdictions: list[Jurisdiction],
    activate: bool = False,
    activate_after_first_chunk: bool = False,
    shards: int = 2,
    chunk_gib: int = DEFAULT_CHUNK_GIB,
    thread_count: int = 12,
    bulk_size: int = 400,
) -> dict[str, Any]:
    if config.search.backend != "opensearch":
        raise ValueError("ingest-full requires search.backend=opensearch")
    index = OpenSearchIndex(config.search)
    if not index.ping():
        raise RuntimeError("OpenSearch is not reachable")

    archive_root = Path(config.storage.archive)
    archive_root.mkdir(parents=True, exist_ok=True)
    selected = [source for source in config.sources if _source_jurisdiction(source) in jurisdictions]
    if not selected:
        raise ValueError(f"no configured sources for {', '.join(item.value for item in jurisdictions)}")

    out_dir = config.storage.normalized / name
    out_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = out_dir / "checkpoint.json"
    checkpoint = _load_checkpoint(checkpoint_path, name)
    inventory = checkpoint.get("inventory") or _inventory(selected)
    skipped: Counter[str] = Counter(checkpoint.get("skipped") or {})
    accepted = int(checkpoint.get("accepted") or 0)
    errors = int(checkpoint.get("errors") or 0)
    processed_rows = int(checkpoint.get("processed_rows") or 0)
    completed = set(checkpoint.get("completed_files") or [])
    chunks: list[dict[str, Any]] = list(checkpoint.get("chunks") or [])
    current_chunk = int(checkpoint.get("current_chunk") or 0)
    by_jurisdiction: Counter[str] = Counter(checkpoint.get("by_jurisdiction") or {})
    limit_bytes = int(chunk_gib) * 1024 * 1024 * 1024
    started = time.perf_counter()
    wall_start = checkpoint.get("started_at") or datetime.now(UTC).isoformat()
    prior_elapsed = float(checkpoint.get("elapsed_s") or 0)

    repo = config.search.snapshot_repo
    location = f"{config.search.snapshot_container_path}/{name}"
    (archive_root / "snapshots" / name).mkdir(parents=True, exist_ok=True)
    index.ensure_snapshot_repo(repo, location)
    current_chunk = _recover_chunk(index, name, current_chunk, chunks)
    chunk_index = index.ensure_chunk(name, current_chunk, shards=shards)

    progress = ProgressLog(
        [
            config.storage.reports / f"{name}-progress.log",
            config.storage.reports / f"{name}-status.json",
            archive_root / f"{name}-progress.log",
            archive_root / f"{name}-status.json",
        ],
        total_rows=int(inventory["rows"]),
        total_files=int(inventory["files"]),
        started_at=wall_start,
    )
    print(
        f"full ingest {name}: chunk={current_chunk} {chunk_index} "
        f"roll>={chunk_gib}GiB threads={thread_count} bulk={bulk_size} "
        f"rows={inventory['rows']:,} files={inventory['files']} "
        f"archive={archive_root}",
        file=sys.stderr,
    )

    def persist(*, finished: bool = False) -> None:
        _write_checkpoint(
            checkpoint_path,
            name=name,
            accepted=accepted,
            errors=errors,
            processed_rows=processed_rows,
            skipped=skipped,
            completed=completed,
            chunks=chunks,
            current_chunk=current_chunk,
            by_jurisdiction=by_jurisdiction,
            inventory=inventory,
            started_at=wall_start,
            elapsed_s=prior_elapsed + (time.perf_counter() - started),
            finished=finished,
        )
        _save_generation(
            store,
            name=name,
            jurisdictions=jurisdictions,
            selected=selected,
            accepted=accepted,
            errors=errors,
            skipped=skipped,
            completed=completed,
            chunks=chunks,
            by_jurisdiction=by_jurisdiction,
            archive=str(archive_root),
            finished=finished,
        )

    for source in selected:
        files = sorted((source.path / "data").glob("*.parquet")) if (source.path / "data").is_dir() else []
        jurisdiction = _source_jurisdiction(source).value
        print(f"{source.repo}: {len(files)} files, {len(completed)} already done", file=sys.stderr)
        for file_index, path in enumerate(files, start=1):
            key = f"{source.repo}:{path.name}"
            if key in completed:
                continue
            file_taken, file_skipped, file_errors = _index_file(
                index,
                source,
                name,
                path,
                chunk_index,
                thread_count=thread_count,
                bulk_size=bulk_size,
            )
            accepted += file_taken
            errors += file_errors
            processed_rows += file_taken + sum(file_skipped.values())
            skipped.update(file_skipped)
            by_jurisdiction[jurisdiction] += file_taken
            completed.add(key)
            store_bytes = index.index_store_bytes(chunk_index)
            elapsed = prior_elapsed + (time.perf_counter() - started)
            progress.write(
                elapsed_s=elapsed,
                processed_rows=processed_rows,
                accepted=accepted,
                errors=errors,
                files_done=len(completed),
                current_file=path.name,
                source=source.repo,
                file_index=file_index,
                source_files=len(files),
                chunk=current_chunk,
                chunk_gb=store_bytes / 1e9,
                chunks_sealed=len(chunks),
                active=store.active_generation(),
            )
            persist()
            if should_roll_chunk(store_bytes, limit_bytes):
                chunks, current_chunk, chunk_index = _seal_chunk(
                    index,
                    repo=repo,
                    generation=name,
                    chunk=current_chunk,
                    chunks=chunks,
                    shards=shards,
                    accepted=accepted,
                )
                persist()

    if index.index_store_bytes(chunk_index) > 0:
        chunks, current_chunk, chunk_index = _seal_chunk(
            index,
            repo=repo,
            generation=name,
            chunk=current_chunk,
            chunks=chunks,
            shards=shards,
            accepted=accepted,
        )

    persist(finished=True)
    if activate:
        print("not activating: restore the snapshots onto the big data disk first (OPENSEARCH_DATA), then court-search restore-archive", file=sys.stderr)
    elapsed = prior_elapsed + (time.perf_counter() - started)
    progress.write(
        elapsed_s=elapsed,
        processed_rows=processed_rows,
        accepted=accepted,
        errors=errors,
        files_done=len(completed),
        current_file="DONE",
        source="all",
        file_index=len(completed),
        source_files=int(inventory["files"]),
        chunk=current_chunk,
        chunk_gb=0.0,
        chunks_sealed=len(chunks),
        active=store.active_generation(),
        finished=True,
    )
    report = {
        "generation": name,
        "counts": {
            "accepted": accepted,
            "index_errors": errors,
            "chunks": len(chunks),
            "files_done": len(completed),
            "by_jurisdiction": dict(by_jurisdiction),
        },
        "chunks": chunks,
        "archive": str(archive_root),
        "activated": store.active_generation() == name,
        "finished": True,
        "progress": str(config.storage.reports / f"{name}-status.json"),
    }
    (config.storage.reports / f"{name}-ingest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return report


def restore_archive(
    config: AppConfig,
    store: StateStore,
    *,
    name: str,
    activate: bool = False,
) -> dict[str, Any]:
    tuning = config.search.model_copy(update={"request_timeout_seconds": 180})
    index = OpenSearchIndex(tuning)
    if not index.ping():
        raise RuntimeError("OpenSearch is not reachable")
    checkpoint = _load_checkpoint(config.storage.normalized / name / "checkpoint.json", name)
    sealed = list(checkpoint.get("chunks") or [])
    if not sealed:
        raise ValueError(f"no sealed chunks for {name}")
    repo = config.search.snapshot_repo
    location = f"{config.search.snapshot_container_path}/{name}"
    index.ensure_snapshot_repo(repo, location)
    restored: list[str] = []
    for chunk in sealed:
        target = chunk["index"]
        snap = chunk.get("snapshot")
        print(f"restore {snap} -> {target}", file=sys.stderr)
        if _index_ready(index, target):
            print(f"  {target} already complete", file=sys.stderr)
        elif snap and _index_recovering(index, target):
            print(f"  waiting for in-progress restore of {target}", file=sys.stderr)
            index._wait_restore(repo, snap, target)
        elif snap:
            if index.client.indices.exists(index=target):
                print(f"  dropping incomplete {target}", file=sys.stderr)
                index.delete_index(target)
            index.restore_snapshot(repo, snap, target)
        if _index_ready(index, target):
            restored.append(target)
            index.point_alias(decision_index_name(name), restored)
            if activate:
                store.activate(name)
            print(f"  searchable {len(restored)}/{len(sealed)} chunks", file=sys.stderr)
    if restored:
        index.point_alias(decision_index_name(name), restored)
    if activate and restored:
        store.activate(name)
    return {"generation": name, "alias": decision_index_name(name), "indices": restored, "activated": activate}


def _index_ready(index: OpenSearchIndex, name: str) -> bool:
    if not index.client.indices.exists(index=name):
        return False
    rows = index.client.cat.indices(index=name, format="json", request_timeout=180)
    if not rows:
        return False
    row = rows[0]
    if (row.get("health") or "") != "green" or (row.get("status") or "") != "open":
        return False
    if int(row.get("docs.count") or 0) <= 0:
        return False
    return not _index_recovering(index, name)


def _index_recovering(index: OpenSearchIndex, name: str) -> bool:
    if not index.client.indices.exists(index=name):
        return False
    recovery = index.client.cat.recovery(index=name, format="json", request_timeout=180)
    return any(item.get("stage") not in {"done", None} for item in (recovery or []))


def _inventory(sources: list[SourceConfig]) -> dict[str, int]:
    files = 0
    rows = 0
    for source in sources:
        data_dir = source.path / "data"
        if not data_dir.is_dir():
            continue
        for path in data_dir.glob("*.parquet"):
            files += 1
            rows += int(pq.ParquetFile(path).metadata.num_rows)
    return {"files": files, "rows": rows}


def _recover_chunk(
    index: OpenSearchIndex, generation: str, current_chunk: int, chunks: list[dict[str, Any]]
) -> int:
    sealed = next((item for item in chunks if item.get("id") == current_chunk and item.get("snapshot")), None)
    if sealed:
        leftover = decision_chunk_index_name(generation, current_chunk)
        print(f"chunk {current_chunk} already snapshotted; dropping leftover live index {leftover}", file=sys.stderr)
        index.delete_index(leftover)
        return current_chunk + 1
    return current_chunk


def _seal_chunk(
    index: OpenSearchIndex,
    *,
    repo: str,
    generation: str,
    chunk: int,
    chunks: list[dict[str, Any]],
    shards: int,
    accepted: int,
) -> tuple[list[dict[str, Any]], int, str]:
    from court_search.indexing.mappings import snapshot_id

    index_name = decision_chunk_index_name(generation, chunk)
    snap = snapshot_id(chunk)
    store_bytes = index.index_store_bytes(index_name)
    print(f"copying {index_name} ({store_bytes / 1e9:.1f}GB) into snapshot {snap}", file=sys.stderr)
    index.finalize_index(index_name)
    if not index.snapshot_exists(repo, snap):
        index.snapshot_index(repo, snap, index_name)
    info = {
        "id": chunk,
        "index": index_name,
        "snapshot": snap,
        "store_bytes": store_bytes,
        "accepted_after": accepted,
        "sealed_at": datetime.now(UTC).isoformat(),
    }
    chunks = [item for item in chunks if item.get("id") != chunk] + [info]
    index.delete_index(index_name)
    nxt = chunk + 1
    next_index = index.ensure_chunk(generation, nxt, shards=shards)
    print(f"live chunk dropped; next {nxt} ({next_index})", file=sys.stderr)
    return chunks, nxt, next_index


def _index_file(
    index: OpenSearchIndex,
    source: SourceConfig,
    generation: str,
    path: Path,
    index_name: str,
    *,
    thread_count: int,
    bulk_size: int,
) -> tuple[int, Counter, int]:
    rejected: Counter[str] = Counter()
    records = _iter_file_records(path, source, rejected)
    result = index.bulk_index(
        generation,
        records,
        finalize=False,
        chunk_size=bulk_size,
        thread_count=thread_count,
        index_name=index_name,
    )
    return int(result["indexed"]), rejected, int(result["errors"])


def _iter_file_records(
    path: Path, source: SourceConfig, rejected: Counter
) -> Iterator[DecisionRecord]:
    parquet = pq.ParquetFile(path)
    missing = [name for name in REQUIRED_COLUMNS if name not in parquet.schema_arrow.names]
    if missing:
        raise ValueError(f"{path} missing required columns: {missing}")
    for group_index in range(parquet.num_row_groups):
        table = parquet.read_row_group(group_index, columns=list(REQUIRED_COLUMNS))
        columns = {name: table.column(name) for name in table.column_names}
        for row_index in range(table.num_rows):
            row = {name: columns[name][row_index].as_py() for name in table.column_names}
            ref = SourceRef(
                repo=source.repo,
                revision=source.revision,
                relative_path=f"data/{path.name}",
                row_group=group_index,
                row_offset=row_index,
            )
            record, reason = build_record(
                row, source=ref, source_jurisdiction=source.jurisdiction, build_paragraphs=False
            )
            if record is None:
                rejected[reason or "rejected"] += 1
                continue
            yield record


def _source_jurisdiction(source: SourceConfig) -> Jurisdiction:
    return source.jurisdiction or _jurisdiction_from_repo(source.repo)


def _load_checkpoint(path: Path, name: str) -> dict[str, Any]:
    if not path.exists():
        return {
            "generation": name,
            "completed_files": [],
            "accepted": 0,
            "errors": 0,
            "processed_rows": 0,
            "skipped": {},
            "chunks": [],
            "current_chunk": 0,
            "by_jurisdiction": {},
        }
    return json.loads(path.read_text(encoding="utf-8"))


def _write_checkpoint(
    path: Path,
    *,
    name: str,
    accepted: int,
    errors: int,
    processed_rows: int,
    skipped: Counter,
    completed: set[str],
    chunks: list[dict[str, Any]],
    current_chunk: int,
    by_jurisdiction: Counter,
    inventory: dict[str, int],
    started_at: str,
    elapsed_s: float,
    finished: bool,
) -> None:
    payload = {
        "generation": name,
        "accepted": accepted,
        "errors": errors,
        "processed_rows": processed_rows,
        "skipped": dict(skipped),
        "completed_files": sorted(completed),
        "chunks": chunks,
        "current_chunk": current_chunk,
        "by_jurisdiction": dict(by_jurisdiction),
        "inventory": inventory,
        "started_at": started_at,
        "elapsed_s": int(elapsed_s),
        "updated_at": datetime.now(UTC).isoformat(),
        "finished": finished,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _save_generation(
    store: StateStore,
    *,
    name: str,
    jurisdictions: list[Jurisdiction],
    selected: list[SourceConfig],
    accepted: int,
    errors: int,
    skipped: Counter,
    completed: set[str],
    chunks: list[dict[str, Any]],
    by_jurisdiction: Counter,
    archive: str,
    finished: bool,
) -> None:
    counts = {
        "accepted": accepted,
        "duplicates": 0,
        "index_errors": errors,
        "quarantined": dict(skipped),
        "by_jurisdiction": dict(by_jurisdiction),
        "files_done": len(completed),
        "chunks": len(chunks),
    }
    notes = [
        "Full corpus: each chunk is built in the live cluster, snapshotted, then deleted from the live cluster.",
        "Restore the snapshots with court-search restore-archive before activating.",
    ]
    if not finished:
        notes.append("Ingest is still running; sealed chunks live in snapshots, not the live alias.")
    store.save_generation(
        name,
        selection_name="full",
        source_manifest={
            "strategy": "full-chunked-nvme",
            "jurisdictions": [item.value for item in jurisdictions],
            "sources": [
                {"repo": source.repo, "path": str(source.path), "revision": source.revision}
                for source in selected
            ],
            "document_store": "opensearch",
            "archive": archive,
            "chunks": chunks,
        },
        counts=counts,
        notes=notes,
        ingested_at=datetime.now(UTC) if finished else None,
    )
