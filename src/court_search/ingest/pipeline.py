from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import pyarrow.parquet as pq
import yaml

from court_search.config import AppConfig, SourceConfig
from court_search.domain.enums import Jurisdiction
from court_search.domain.models import DecisionRecord, SourceRef
from court_search.ingest.canonical import REQUIRED_COLUMNS, build_record
from court_search.ingest.select import PILOT_QUOTA, allocate_file_quotas, file_row_counts, scale_quotas, stride_indices
from court_search.storage.db import StateStore

SLICE_QUOTA: dict[Jurisdiction, int] = {
    Jurisdiction.CIVIL: 200,
    Jurisdiction.ADMINISTRATIVE: 50,
    Jurisdiction.CRIMINAL: 50,
    Jurisdiction.COMMERCIAL: 50,
    Jurisdiction.ADMINISTRATIVE_OFFENSE: 50,
}


def ingest_slice(
    config: AppConfig,
    store: StateStore,
    *,
    name: str = "slice-v1",
    limit: int = 400,
) -> dict[str, Any]:
    return ingest_corpus(
        config,
        store,
        name=name,
        limit=limit,
        strategy="head",
        activate=True,
        rebuild_fts=True,
    )


def ingest_corpus(
    config: AppConfig,
    store: StateStore,
    *,
    name: str,
    limit: int,
    strategy: Literal["head", "stratified"] = "stratified",
    activate: bool = False,
    rebuild_fts: bool = False,
) -> dict[str, Any]:
    base = PILOT_QUOTA if strategy == "stratified" else SLICE_QUOTA
    quotas = scale_quotas(limit, base)
    store.clear_decisions(name)
    out_dir = config.storage.normalized / name
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = out_dir / "decisions.jsonl"
    manifest_ids: list[str] = []
    quarantined = Counter()
    by_jurisdiction: Counter[str] = Counter()
    source_manifest: list[dict[str, Any]] = []
    duplicates = 0
    seen: set[str] = set()
    accepted = 0

    with jsonl_path.open("w", encoding="utf-8") as handle:
        for source in config.sources:
            jurisdiction = source.jurisdiction or _jurisdiction_from_repo(source.repo)
            want = quotas.get(jurisdiction, 0)
            if want <= 0:
                continue
            print(f"ingest {source.repo}: target {want}", file=sys.stderr)
            taken, skipped, files, dupes = _ingest_source(
                source,
                store,
                generation=name,
                limit=want,
                strategy=strategy,
                seen=seen,
                jsonl=handle,
                seed=f"{name}:{source.repo}",
            )
            accepted += taken
            duplicates += dupes
            quarantined.update(skipped)
            by_jurisdiction[jurisdiction.value] += taken
            source_manifest.append(
                {
                    "repo": source.repo,
                    "path": str(source.path),
                    "revision": source.revision,
                    "files_sampled": files,
                    "accepted": taken,
                }
            )
            store.conn.commit()

    if rebuild_fts:
        store.rebuild_fts(name)
    counts = {
        "accepted": accepted,
        "duplicates": duplicates,
        "quarantined": dict(quarantined),
        "by_jurisdiction": dict(by_jurisdiction),
    }
    notes = [
        f"Selection strategy={strategy}. Lexical search uses the configured backend.",
        "Presence in this snapshot is not completeness of the live registry.",
    ]
    store.save_generation(
        name,
        selection_name=strategy,
        source_manifest={"sources": source_manifest, "strategy": strategy, "limit": limit},
        counts=counts,
        notes=notes,
        ingested_at=datetime.now(UTC),
    )
    if activate:
        store.activate(name)
    _harvest_dictionary_codes(config.dictionaries_dir, store, name)
    id_manifest = {
        "name": name,
        "strategy": strategy,
        "limit": limit,
        "count": accepted,
        "by_jurisdiction": dict(by_jurisdiction),
    }
    (out_dir / "selection.json").write_text(json.dumps(id_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    report = {
        "generation": name,
        "counts": counts,
        "sources": source_manifest,
        "activated": activate,
    }
    report_path = config.storage.reports / f"{name}-ingest.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return report


def _ingest_source(
    source: SourceConfig,
    store: StateStore,
    *,
    generation: str,
    limit: int,
    strategy: str,
    seen: set[str],
    jsonl,
    seed: str,
) -> tuple[int, Counter, list[str], int]:
    data_dir = source.path / "data"
    if not data_dir.is_dir():
        return 0, Counter({"missing_source": 1}), [], 0
    files = sorted(path for path in data_dir.glob("*.parquet"))
    skipped: Counter = Counter()
    used_files: list[str] = []
    taken = 0
    duplicates = 0
    if strategy == "head":
        for path in files:
            if taken >= limit:
                break
            used_files.append(path.name)
            batch, batch_skipped = _read_parquet_head(path, source, limit - taken)
            written, dups, batch_skipped2 = _persist_batch(batch, store, generation, seen, jsonl)
            taken += written
            duplicates += dups
            skipped.update(batch_skipped)
            skipped.update(batch_skipped2)
        return taken, skipped, used_files, duplicates

    counts = file_row_counts(files)
    plan = allocate_file_quotas(counts, limit)
    for path, rows, quota in plan:
        if taken >= limit:
            break
        used_files.append(path.name)
        indices = stride_indices(rows, min(quota, limit - taken), seed=f"{seed}:{path.name}")
        batch, batch_skipped = _read_parquet_indices(path, source, indices)
        written, dups, extra_skipped = _persist_batch(batch, store, generation, seen, jsonl)
        taken += written
        duplicates += dups
        skipped.update(batch_skipped)
        skipped.update(extra_skipped)
        if taken and taken % 10_000 < quota:
            print(f"  {source.repo} accepted {taken}/{limit}", file=sys.stderr)
    return taken, skipped, used_files, duplicates


def _persist_batch(
    records: list[DecisionRecord],
    store: StateStore,
    generation: str,
    seen: set[str],
    jsonl,
) -> tuple[int, int, Counter]:
    written = 0
    duplicates = 0
    skipped: Counter = Counter()
    for record in records:
        if record.doc_id in seen:
            duplicates += 1
            continue
        seen.add(record.doc_id)
        store.insert_decision(generation, record)
        jsonl.write(record.model_dump_json() + "\n")
        written += 1
    return written, duplicates, skipped


def _jurisdiction_from_repo(repo: str) -> Jurisdiction:
    suffix = repo.rsplit("-", 1)[-1]
    return {
        "tspk": Jurisdiction.CIVIL,
        "kas": Jurisdiction.ADMINISTRATIVE,
        "kpk": Jurisdiction.CRIMINAL,
        "gpk": Jurisdiction.COMMERCIAL,
        "kupap": Jurisdiction.ADMINISTRATIVE_OFFENSE,
    }[suffix]


def _read_parquet_head(
    path: Path, source: SourceConfig, limit: int
) -> tuple[list[DecisionRecord], Counter]:
    return _read_parquet_indices(path, source, list(range(limit)))


def _read_parquet_indices(
    path: Path, source: SourceConfig, indices: list[int]
) -> tuple[list[DecisionRecord], Counter]:
    if not indices:
        return [], Counter()
    wanted = set(indices)
    parquet = pq.ParquetFile(path)
    missing = [name for name in REQUIRED_COLUMNS if name not in parquet.schema_arrow.names]
    if missing:
        raise ValueError(f"{path} missing required columns: {missing}")
    taken: list[DecisionRecord] = []
    skipped: Counter = Counter()
    absolute = 0
    max_index = max(wanted)
    for group_index in range(parquet.num_row_groups):
        if absolute > max_index:
            break
        table = parquet.read_row_group(group_index, columns=list(REQUIRED_COLUMNS))
        columns = {name: table.column(name) for name in table.column_names}
        for row_index in range(table.num_rows):
            if absolute in wanted:
                row = {name: columns[name][row_index].as_py() for name in table.column_names}
                ref = SourceRef(
                    repo=source.repo,
                    revision=source.revision,
                    relative_path=f"data/{path.name}",
                    row_group=group_index,
                    row_offset=row_index,
                )
                record, reason = build_record(
                    row,
                    source=ref,
                    source_jurisdiction=source.jurisdiction,
                )
                if record is None:
                    skipped[reason or "rejected"] += 1
                else:
                    taken.append(record)
            absolute += 1
            if absolute > max_index:
                break
    return taken, skipped


def _harvest_dictionary_codes(root: Path, store: StateStore, generation: str) -> None:
    harvested = store.harvest_codes(generation)
    _merge_codes(root / "courts.yaml", harvested["court"])
    _merge_codes(root / "judgment_types.yaml", harvested["judgment"])
    _merge_codes(root / "categories.yaml", harvested["category"])


def _merge_codes(path: Path, codes: set[str]) -> None:
    if not path.exists():
        return
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    labels = data.setdefault("labels", {})
    changed = False
    for code in sorted(codes):
        if str(code) not in labels:
            labels[str(code)] = None
            changed = True
    if changed:
        path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
