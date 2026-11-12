from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow.parquet as pq

from court_search.config import AppConfig
from court_search.embed.run import EMBED_SPEC_NAME, _section_dir, shard_files
from court_search.indexing.opensearch import OpenSearchIndex


def _shard_root(config: AppConfig, section_id: str, spec_name: str) -> Path:
    archive = config.storage.archive / "embeddings" / spec_name / section_id
    if (archive / "shards").is_dir() and any((archive / "shards").glob("*.parquet")):
        return archive
    return _section_dir(config, section_id, spec_name=spec_name)


def merge_loader_reports(
    config: AppConfig, *, section_id: str, spec_name: str = EMBED_SPEC_NAME, from_dir: Path | None = None
) -> dict[str, Any]:
    """Fold per-loader os-index-<k>of<n>.json into os-index.json (run while no loader is active).

    Afterwards the section can be re-split into any number of loaders without re-sending
    files that are already in the index.
    """
    out_dir = Path(from_dir) if from_dir is not None else _shard_root(config, section_id, spec_name=spec_name)
    main = out_dir / "os-index.json"
    merged = json.loads(main.read_text(encoding="utf-8")) if main.exists() else {}
    files = list(merged.get("files") or [])
    seen = set(files)
    indexed = int(merged.get("indexed") or 0)
    errors = int(merged.get("errors") or 0)
    parts = sorted(out_dir.glob("os-index-*of*.json"))
    for part in parts:
        data = json.loads(part.read_text(encoding="utf-8"))
        new = [name for name in data.get("files") or [] if name not in seen]
        files += new
        seen.update(new)
        indexed += int(data.get("indexed") or 0)
        errors += int(data.get("errors") or 0)
    _write_report(main, index=merged.get("index") or f"passages-{spec_name}", section_id=section_id,
                  files=files, indexed=indexed, errors=errors, count=int(merged.get("count") or 0))
    for part in parts:
        part.rename(part.with_suffix(".json.merged"))
    return {"section": section_id, "files": len(files), "indexed": indexed, "errors": errors, "merged_parts": len(parts)}


def index_passage_shards(
    config: AppConfig,
    *,
    section_id: str,
    spec_name: str = EMBED_SPEC_NAME,
    limit_files: int | None = None,
    min_passages: int = 0,
    recreate: bool = False,
    resume: bool = True,
    refresh_every: int = 0,
    file_attempts: int = 6,
    shard: tuple[int, int] | None = None,
    from_dir: Path | None = None,
) -> dict[str, Any]:
    """Load embedding parquet into an OpenSearch knn index. Resumes by file name.

    By default reads our shard layout (<spec dir>/<section>/shards). ``from_dir`` reads
    plain parquet files from any directory instead, e.g. a download of the published
    Hugging Face dataset (data/<section>/*.parquet); progress is kept in that directory.

    ``shard=(k, n)`` loads the k-th of n size-balanced groups of files, so n loaders can
    run side by side. Each keeps its own ``os-index-<k>of<n>.json`` and also skips files
    already listed in the section's main ``os-index.json``.
    """
    index = OpenSearchIndex(config.search)
    if not index.ping():
        raise RuntimeError("OpenSearch is not reachable")
    if from_dir is not None:
        out_dir = Path(from_dir)
        shards = sorted(out_dir.glob("*.parquet"))
    else:
        out_dir = _shard_root(config, section_id, spec_name=spec_name)
        shards = sorted((out_dir / "shards").glob("*.parquet"))
    if not shards:
        raise ValueError(f"no parquet files under {out_dir if from_dir else out_dir / 'shards'}")
    chosen = []
    for path in shards:
        rows = pq.ParquetFile(path).metadata.num_rows
        if min_passages and rows < min_passages:
            continue
        chosen.append(path)
    if shard is not None:
        if recreate:
            raise ValueError("recreate cannot be combined with shard (other loaders share the index)")
        chosen = shard_files(chosen, *shard)
    main_report = out_dir / "os-index.json"
    report_path = main_report if shard is None else out_dir / f"os-index-{shard[0]}of{shard[1]}.json"
    done: list[str] = []
    indexed = 0
    errors = 0
    if resume and not recreate and report_path.exists():
        previous = json.loads(report_path.read_text(encoding="utf-8"))
        done = list(previous.get("files") or [])
        indexed = int(previous.get("indexed") or 0)
        errors = int(previous.get("errors") or 0)
    skip = set(done)
    if resume and not recreate and shard is not None and main_report.exists():
        skip |= set(json.loads(main_report.read_text(encoding="utf-8")).get("files") or [])
    remaining = [path for path in chosen if path.name not in skip]
    if limit_files is not None:
        remaining = remaining[: max(0, limit_files)]
    name = index.ensure_passage_index(spec_name=spec_name, recreate=recreate)
    if recreate:
        done = []
        indexed = 0
        errors = 0
    added_now = 0
    for offset, path in enumerate(remaining, start=1):
        added, failed = _load_file_with_retries(index, name, path, section_id, attempts=file_attempts)
        indexed += added
        added_now += added
        errors += failed
        done.append(path.name)
        _write_report(
            report_path,
            index=name,
            section_id=section_id,
            files=done,
            indexed=indexed,
            errors=errors,
            count=_safe_count(index, spec_name),
        )
        print(
            f"{path.name} indexed=+{added} errors={failed} files={len(done)}/{len(chosen)} total={indexed}",
            flush=True,
        )
        # The index refreshes itself (refresh_interval); an explicit refresh on a large,
        # busy index can take minutes, so it is opt-in and never fatal.
        if refresh_every and offset % refresh_every == 0:
            _safe_refresh(index, name)
    if remaining and refresh_every:
        _safe_refresh(index, name)
    count = _safe_count(index, spec_name)
    _write_report(
        report_path,
        index=name,
        section_id=section_id,
        files=done,
        indexed=indexed,
        errors=errors,
        count=count,
    )
    return {
        "index": name,
        "section": section_id,
        "files": done,
        "indexed": indexed,
        "indexed_this_run": added_now,
        "errors": errors,
        "count": count,
        "remaining": len(chosen) - len(done),
    }


def _write_report(path: Path, *, index: str, section_id: str, files: list[str], indexed: int, errors: int, count: int) -> None:
    path.write_text(
        json.dumps(
            {
                "index": index,
                "section": section_id,
                "files": files,
                "indexed": indexed,
                "errors": errors,
                "count": count,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _load_file_with_retries(index: OpenSearchIndex, name: str, path: Path, section_id: str, *, attempts: int) -> tuple[int, int]:
    """Bulk-load one shard; on a timeout or dropped connection wait and send the file again.

    Re-sending is safe: documents use passage_id as _id, so they overwrite, never duplicate.
    """
    from opensearchpy.exceptions import ConnectionError as OSConnectionError
    from opensearchpy.exceptions import ConnectionTimeout, TransportError

    for attempt in range(1, attempts + 1):
        try:
            return index.bulk_passages(name, _iter_passage_docs(path, section_id))
        except (ConnectionTimeout, OSConnectionError, TransportError) as exc:
            if attempt == attempts:
                raise
            wait = min(600, 30 * 2 ** (attempt - 1))
            print(f"{path.name}: {type(exc).__name__} on attempt {attempt}; retrying the file in {wait}s", flush=True)
            time.sleep(wait)
    raise RuntimeError("unreachable")


def _safe_count(index: OpenSearchIndex, spec_name: str) -> int:
    try:
        return index.passage_count(spec_name)
    except Exception as exc:  # informational only: never stop a load over it
        print(f"passage count unavailable ({type(exc).__name__}); continuing", flush=True)
        return -1


def _safe_refresh(index: OpenSearchIndex, name: str) -> None:
    try:
        index.client.indices.refresh(index=name, request_timeout=600)
    except Exception as exc:
        print(f"refresh skipped ({type(exc).__name__}); the index refreshes on its own", flush=True)


INDEX_COLUMNS = ["passage_id", "doc_id", "jurisdiction", "char_start", "char_end", "vector"]


def _iter_passage_docs(path: Path, section_id: str):
    parquet = pq.ParquetFile(path)
    # Only what the index stores: skipping the passage text saves ~1/3 of the read.
    columns = [name for name in INDEX_COLUMNS if name in parquet.schema_arrow.names]
    for group in range(parquet.num_row_groups):
        table = parquet.read_row_group(group, columns=columns)
        cols = {name: table.column(name) for name in table.column_names}
        for row in range(table.num_rows):
            raw = cols["vector"][row].as_py()
            if not raw:
                continue
            # Our shards store fp16 bytes; the published dataset stores a float32 list.
            if isinstance(raw, (bytes, bytearray)):
                vector = np.frombuffer(raw, dtype=np.float16).astype(np.float32).tolist()
            else:
                vector = [float(x) for x in raw]
            if len(vector) != 1024:
                continue
            passage_id = str(cols["passage_id"][row].as_py())
            yield {
                "_op_type": "index",
                "_id": passage_id,
                "passage_id": passage_id,
                "doc_id": str(cols["doc_id"][row].as_py()),
                "jurisdiction": cols["jurisdiction"][row].as_py(),
                "section": section_id,
                "char_start": int(cols["char_start"][row].as_py() or 0),
                "char_end": int(cols["char_end"][row].as_py() or 0),
                "vector": vector,
            }
