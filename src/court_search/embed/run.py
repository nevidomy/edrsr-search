from __future__ import annotations

import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterator

import pyarrow as pa
import pyarrow.parquet as pq

from court_search.config import AppConfig, SourceConfig
from court_search.embed.chunker import CHUNKING_VERSION, chunk_text, estimate_tokens
from court_search.embed.sections import SECTION_BY_ID, SECTIONS
from court_search.ingest.canonical import as_optional_str, sha256_text
from court_search.ingest.pipeline import _jurisdiction_from_repo

MODEL_ID = "BAAI/bge-m3"
DIMENSION = 1024
EMBED_SPEC_NAME = "bge-m3-dense-v1"


def list_sections() -> list[dict[str, Any]]:
    return [
        {
            "id": item["id"],
            "folder": item["folder"],
            "repo": item["repo"],
            "rows": item["rows"],
            "note": item["note"],
        }
        for item in SECTIONS
    ]


def chunk_section(
    config: AppConfig,
    *,
    section_id: str,
    limit_files: int | None = None,
    spec_name: str = EMBED_SPEC_NAME,
    shard: tuple[int, int] | None = None,
    worker: str | None = None,
) -> dict[str, Any]:
    return _run_section(
        config,
        section_id=section_id,
        encode=False,
        limit_files=limit_files,
        spec_name=spec_name,
        shard=shard,
        worker=worker,
    )


def embed_section(
    config: AppConfig,
    *,
    section_id: str,
    batch_size: int,
    max_length: int = 1000,
    device: str | None = None,
    limit_files: int | None = None,
    min_source_rows: int | None = None,
    max_passages: int | None = None,
    spec_name: str = EMBED_SPEC_NAME,
    shard: tuple[int, int] | None = None,
    worker: str | None = None,
) -> dict[str, Any]:
    return _run_section(
        config,
        section_id=section_id,
        encode=True,
        batch_size=batch_size,
        max_length=max_length,
        device=device,
        limit_files=limit_files,
        min_source_rows=min_source_rows,
        max_passages=max_passages,
        spec_name=spec_name,
        shard=shard,
        worker=worker,
    )


def _run_section(
    config: AppConfig,
    *,
    section_id: str,
    encode: bool,
    batch_size: int = 16,
    max_length: int = 1000,
    device: str | None = None,
    limit_files: int | None = None,
    min_source_rows: int | None = None,
    max_passages: int | None = None,
    spec_name: str = EMBED_SPEC_NAME,
    shard: tuple[int, int] | None = None,
    worker: str | None = None,
) -> dict[str, Any]:
    """Encode (or only chunk) one section, resumable per source file.

    ``shard=(k, n)`` keeps the k-th of n size-balanced file groups, so n machines
    can split a section without talking to each other. ``worker`` lets several
    processes on one machine share the same output directory: each claims files
    atomically under ``claims/`` and reports progress to ``workers/<name>.json``.
    """
    section = SECTION_BY_ID.get(section_id)
    if section is None:
        raise ValueError(f"unknown section {section_id}; choose from {list(SECTION_BY_ID)}")
    source = _source_for_section(config, section)
    files = sorted((source.path / "data").glob("*.parquet")) if (source.path / "data").is_dir() else []
    if not files:
        raise ValueError(f"no parquet under {source.path / 'data'}")
    if min_source_rows:
        files = [path for path in files if int(pq.ParquetFile(path).metadata.num_rows) >= min_source_rows]
        if not files:
            raise ValueError(f"no parquet with at least {min_source_rows} rows under {source.path / 'data'}")
    if limit_files is not None:
        files = files[: max(0, limit_files)]
    if shard is not None:
        files = shard_files(files, *shard)

    out_dir = _section_dir(config, section_id, spec_name=spec_name)
    (out_dir / "files").mkdir(parents=True, exist_ok=True)
    (out_dir / "shards").mkdir(parents=True, exist_ok=True)
    if worker:
        (out_dir / "claims").mkdir(parents=True, exist_ok=True)
        (out_dir / "workers").mkdir(parents=True, exist_ok=True)
    encoder = _load_encoder(batch_size=batch_size, max_length=max_length, device=device) if encode else None
    started = time.perf_counter()
    states = _load_file_states(out_dir)
    already = sum(1 for path in files if _is_done(states.get(path.name), encode, out_dir))

    _write_spec(out_dir, spec_name=spec_name, encode=encode, batch_size=batch_size, max_length=max_length, device=device)
    if not worker:
        _write_checkpoint(out_dir, section_id=section_id, files=files, states=states, encode=encode, elapsed_s=0)

    print(
        f"{'embed' if encode else 'chunk'} {section_id}: files={len(files)} "
        f"already={already} batch={batch_size if encode else '-'} "
        f"shard={'/'.join(map(str, shard)) if shard else '-'} worker={worker or '-'} out={out_dir}",
        file=sys.stderr,
    )
    # Workers take the largest files first so the tail of the run stays short.
    order = sorted(files, key=lambda item: item.stat().st_size, reverse=True) if worker else files
    mine = {"files_done": 0, "passages": 0, "encode_s": 0.0}
    for path in order:
        existing = states.get(path.name)
        if _is_done(existing, encode, out_dir):
            continue
        if worker:
            if _is_done(_read_state(out_dir, path), encode, out_dir):
                continue
            if not _claim(out_dir, path, worker):
                continue
            _write_worker_progress(out_dir, worker, section_id, current=path.name, **mine)
        file_started = time.perf_counter()
        rows: list[dict[str, Any]] = []
        for row in _iter_passages(path, source, estimate_tokens):
            rows.append(row)
            if max_passages and len(rows) >= max_passages:
                break
        vectors = encoder.encode([row["text"] for row in rows]) if encoder and rows else None
        file_s = time.perf_counter() - file_started
        shard_rel = f"shards/{path.stem}.parquet"
        shard_path = out_dir / shard_rel
        _write_shard(shard_path, rows, vectors)
        state = {
            "source_file": path.name,
            "source_size": path.stat().st_size,
            "source_rows": int(pq.ParquetFile(path).metadata.num_rows),
            "shard": shard_rel,
            "state_file": f"files/{path.stem}.json",
            "passages": len(rows),
            "documents": len({row["doc_id"] for row in rows}),
            "has_vectors": bool(encode),
            "chunking_version": CHUNKING_VERSION,
            "status": "done",
            "updated_at": datetime.now(UTC).isoformat(),
        }
        _atomic_write_json(out_dir / "files" / f"{path.stem}.json", state)
        states[path.name] = state
        elapsed = time.perf_counter() - started
        rate = (len(rows) / file_s) if file_s > 0 else 0
        if worker:
            mine["files_done"] += 1
            mine["passages"] += len(rows)
            mine["encode_s"] += file_s
            _write_worker_progress(out_dir, worker, section_id, current=None, **mine)
            print(f"{path.name} passages={len(rows)} {rate:.0f}/s worker={worker} mine={mine['files_done']}", file=sys.stderr)
            continue
        _write_checkpoint(
            out_dir,
            section_id=section_id,
            files=files,
            states=states,
            encode=encode,
            elapsed_s=elapsed,
        )
        print(
            f"{path.name} passages={len(rows)} {rate:.0f}/s "
            f"done={sum(1 for item in files if _is_done(states.get(item.name), encode, out_dir))}/{len(files)}",
            file=sys.stderr,
        )

    if worker:
        # Other workers wrote their own file states; rebuild totals from disk.
        states = _load_file_states(out_dir)
    checkpoint = _write_checkpoint(
        out_dir,
        section_id=section_id,
        files=files,
        states=states,
        encode=encode,
        elapsed_s=time.perf_counter() - started,
        finished=True,
    )
    report = {
        "section": section_id,
        "output": str(out_dir),
        **{key: checkpoint[key] for key in ("encode", "files_done", "files_total", "documents", "passages")},
    }
    _atomic_write_json(out_dir / "report.json", report)
    return report


def rebuild_checkpoint(
    config: AppConfig,
    *,
    section_id: str,
    spec_name: str = EMBED_SPEC_NAME,
    shard: tuple[int, int] | None = None,
) -> dict[str, Any]:
    """Rewrite checkpoint.json/report.json from per-file state on disk (no GPU, no encoding)."""
    section = SECTION_BY_ID[section_id]
    source = _source_for_section(config, section)
    files = sorted((source.path / "data").glob("*.parquet"))
    if shard is not None:
        files = shard_files(files, *shard)
    out_dir = _section_dir(config, section_id, spec_name=spec_name)
    states = _load_file_states(out_dir)
    checkpoint = _write_checkpoint(
        out_dir, section_id=section_id, files=files, states=states, encode=True, elapsed_s=0, finished=True
    )
    report = {
        "section": section_id,
        "output": str(out_dir),
        **{key: checkpoint[key] for key in ("encode", "files_done", "files_total", "documents", "passages")},
    }
    _atomic_write_json(out_dir / "report.json", report)
    return report


def parse_shard(value: str | None) -> tuple[int, int] | None:
    """Parse ``"k/n"`` (0-based k) into ``(k, n)``."""
    if value is None or value == "":
        return None
    try:
        k_text, n_text = value.split("/", 1)
        k, n = int(k_text), int(n_text)
    except ValueError as exc:
        raise ValueError(f"shard must look like k/n, got {value!r}") from exc
    if n < 1 or not 0 <= k < n:
        raise ValueError(f"shard {value!r}: need 0 <= k < n")
    return k, n


def shard_files(files: list[Path], k: int, n: int) -> list[Path]:
    """Deterministic size-balanced split: largest file to the lightest group.

    Every machine computes the same assignment from the same file set, so no
    coordination is needed. Returns group ``k`` in name order.
    """
    if n == 1:
        return list(files)
    loads = [0] * n
    groups: list[list[Path]] = [[] for _ in range(n)]
    for path in sorted(files, key=lambda item: (-item.stat().st_size, item.name)):
        target = min(range(n), key=lambda index: (loads[index], index))
        groups[target].append(path)
        loads[target] += path.stat().st_size
    return sorted(groups[k], key=lambda item: item.name)


def _claim(out_dir: Path, path: Path, worker: str) -> bool:
    """Atomically claim a source file. A worker may re-take its own claim after a restart."""
    claim = out_dir / "claims" / f"{path.stem}.claim"
    try:
        fd = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        try:
            return claim.read_text(encoding="utf-8").strip() == worker
        except OSError:
            return False
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(worker)
    return True


def _read_state(out_dir: Path, path: Path) -> dict[str, Any] | None:
    state_path = out_dir / "files" / f"{path.stem}.json"
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_worker_progress(
    out_dir: Path,
    worker: str,
    section_id: str,
    *,
    current: str | None,
    files_done: int,
    passages: int,
    encode_s: float,
) -> None:
    _atomic_write_json(
        out_dir / "workers" / f"{worker}.json",
        {
            "worker": worker,
            "section": section_id,
            "current": current,
            "files_done": files_done,
            "passages": passages,
            "passages_per_s": round(passages / encode_s, 1) if encode_s > 0 else None,
            "updated_at": datetime.now(UTC).isoformat(),
        },
    )


def _is_done(state: dict[str, Any] | None, encode: bool, out_dir: Path) -> bool:
    if not state or state.get("status") != "done":
        return False
    if encode and not state.get("has_vectors"):
        return False
    shard = state.get("shard")
    return bool(shard) and (out_dir / str(shard)).is_file()


def _load_file_states(out_dir: Path) -> dict[str, dict[str, Any]]:
    states: dict[str, dict[str, Any]] = {}
    files_dir = out_dir / "files"
    if not files_dir.is_dir():
        return states
    for path in sorted(files_dir.glob("*.json")):
        state = json.loads(path.read_text(encoding="utf-8"))
        shard = out_dir / str(state.get("shard") or "")
        if state.get("status") == "done" and not shard.is_file():
            state["status"] = "incomplete"
        source = str(state.get("source_file") or f"{path.stem}.parquet")
        states[source] = state
    return states


def _write_checkpoint(
    out_dir: Path,
    *,
    section_id: str,
    files: list[Path],
    states: dict[str, dict[str, Any]],
    encode: bool,
    elapsed_s: float,
    finished: bool = False,
) -> dict[str, Any]:
    done = [path.name for path in files if _is_done(states.get(path.name), encode, out_dir)]
    payload = {
        "section": section_id,
        "spec_dir": str(out_dir),
        "encode": encode,
        "chunking_version": CHUNKING_VERSION,
        "files_total": len(files),
        "files_done": len(done),
        "already_complete": len(done),
        "documents": sum(int(states[name]["documents"]) for name in done if name in states),
        "passages": sum(int(states[name]["passages"]) for name in done if name in states),
        "completed_files": done,
        "files": {
            path.name: {
                "status": (states.get(path.name) or {}).get("status", "pending"),
                "shard": (states.get(path.name) or {}).get("shard"),
                "state_file": f"files/{path.stem}.json",
                "passages": (states.get(path.name) or {}).get("passages", 0),
                "has_vectors": (states.get(path.name) or {}).get("has_vectors", False),
            }
            for path in files
        },
        "elapsed_s": int(elapsed_s),
        "updated_at": datetime.now(UTC).isoformat(),
        "finished": finished and len(done) == len(files),
    }
    _atomic_write_json(out_dir / "checkpoint.json", payload)
    return payload


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Per-process temp name: several workers may finish and rewrite checkpoint.json at once.
    tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _source_for_section(config: AppConfig, section: dict[str, Any]) -> SourceConfig:
    wanted = section["jurisdiction"]
    for source in config.sources:
        jurisdiction = source.jurisdiction or _jurisdiction_from_repo(source.repo)
        if jurisdiction == wanted:
            return source
    raise ValueError(f"no configured source for {section['id']}")


def _section_dir(config: AppConfig, section_id: str, *, spec_name: str = EMBED_SPEC_NAME) -> Path:
    return config.storage.embeddings / spec_name / section_id


def _write_spec(
    out_dir: Path,
    *,
    spec_name: str,
    encode: bool,
    batch_size: int,
    max_length: int,
    device: str | None,
) -> None:
    spec = {
        "name": spec_name,
        "model": MODEL_ID,
        "dimension": DIMENSION,
        "normalize": True,
        "numeric": "float16",
        "chunking_version": CHUNKING_VERSION,
        "encode": encode,
        "batch_size": batch_size,
        "max_length": max_length,
        "device": device,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    _atomic_write_json(out_dir / "spec.json", spec)


def _iter_passages(
    path: Path,
    source: SourceConfig,
    count_tokens: Callable[[str], int],
) -> Iterator[dict[str, Any]]:
    parquet = pq.ParquetFile(path)
    missing = [name for name in ("doc_id", "text") if name not in parquet.schema_arrow.names]
    if missing:
        raise ValueError(f"{path} missing {missing}")
    columns = [name for name in ("doc_id", "text", "judgment_code") if name in parquet.schema_arrow.names]
    jurisdiction = (source.jurisdiction or _jurisdiction_from_repo(source.repo)).value
    for group_index in range(parquet.num_row_groups):
        table = parquet.read_row_group(group_index, columns=columns)
        col = {name: table.column(name) for name in table.column_names}
        for row_index in range(table.num_rows):
            text = col["text"][row_index].as_py()
            if text is None or not str(text).strip():
                continue
            text = str(text)
            doc_id = as_optional_str(col["doc_id"][row_index].as_py())
            if not doc_id:
                continue
            text_hash = sha256_text(text)
            judgment = None
            if "judgment_code" in col:
                raw = col["judgment_code"][row_index].as_py()
                judgment = None if raw is None else str(int(raw) if isinstance(raw, (int, float)) else raw)
            for passage in chunk_text(text, doc_id=doc_id, text_hash=text_hash, count_tokens=count_tokens):
                passage["jurisdiction"] = jurisdiction
                passage["judgment_code"] = judgment
                passage["source_file"] = path.name
                passage["row_group"] = group_index
                passage["row_offset"] = row_index
                yield passage


def _write_shard(path: Path, rows: list[dict[str, Any]], vectors: Any | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    if not rows:
        table = pa.table({"passage_id": pa.array([], type=pa.string())})
    else:
        payload = {key: [row.get(key) for row in rows] for key in rows[0]}
        if vectors is not None:
            payload["vector"] = [vec.astype("float16").tobytes() for vec in vectors]
        table = pa.table(payload)
    pq.write_table(table, tmp)
    os.replace(tmp, path)


class _Encoder:
    def __init__(self, model: Any, batch_size: int, max_length: int, device: str) -> None:
        self.model = model
        self.batch_size = batch_size
        self.max_length = max_length
        self.device = device

    def encode(self, texts: list[str]) -> list[Any]:
        if not texts:
            return []
        return list(
            self.model.encode(
                texts,
                batch_size=self.batch_size,
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=len(texts) >= 2048,
            )
        )


def _load_encoder(*, batch_size: int, max_length: int, device: str | None) -> _Encoder:
    try:
        import torch
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise RuntimeError(
            "GPU extra missing. On the GPU machine: pip install -e '.[gpu]' "
            "or pip install -r deployment/gpu/requirements.txt"
        ) from exc
    chosen = device or ("cuda" if torch.cuda.is_available() else "cpu")
    print(f"loading {MODEL_ID} on {chosen} batch={batch_size} max_length={max_length} fp16", file=sys.stderr)
    kwargs: dict[str, Any] = {"device": chosen}
    try:
        model = SentenceTransformer(MODEL_ID, model_kwargs={"torch_dtype": torch.float16}, **kwargs)
    except TypeError:
        model = SentenceTransformer(MODEL_ID, **kwargs)
        if chosen == "cuda":
            model = model.half()
    model.max_seq_length = max_length
    return _Encoder(model, batch_size, max_length, chosen)
