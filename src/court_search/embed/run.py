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
) -> dict[str, Any]:
    return _run_section(
        config, section_id=section_id, encode=False, limit_files=limit_files, spec_name=spec_name
    )


def embed_section(
    config: AppConfig,
    *,
    section_id: str,
    batch_size: int,
    max_length: int = 1000,
    device: str | None = None,
    limit_files: int | None = None,
    spec_name: str = EMBED_SPEC_NAME,
) -> dict[str, Any]:
    return _run_section(
        config,
        section_id=section_id,
        encode=True,
        batch_size=batch_size,
        max_length=max_length,
        device=device,
        limit_files=limit_files,
        spec_name=spec_name,
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
    spec_name: str = EMBED_SPEC_NAME,
) -> dict[str, Any]:
    section = SECTION_BY_ID.get(section_id)
    if section is None:
        raise ValueError(f"unknown section {section_id}; choose from {list(SECTION_BY_ID)}")
    source = _source_for_section(config, section)
    files = sorted((source.path / "data").glob("*.parquet")) if (source.path / "data").is_dir() else []
    if not files:
        raise ValueError(f"no parquet under {source.path / 'data'}")
    if limit_files is not None:
        files = files[: max(0, limit_files)]

    out_dir = _section_dir(config, section_id, spec_name=spec_name)
    (out_dir / "files").mkdir(parents=True, exist_ok=True)
    (out_dir / "shards").mkdir(parents=True, exist_ok=True)
    encoder = _load_encoder(batch_size=batch_size, max_length=max_length, device=device) if encode else None
    started = time.perf_counter()
    states = _load_file_states(out_dir)
    already = sum(1 for path in files if _is_done(states.get(path.name), encode, out_dir))

    _write_spec(out_dir, spec_name=spec_name, encode=encode, batch_size=batch_size, max_length=max_length, device=device)
    _write_checkpoint(out_dir, section_id=section_id, files=files, states=states, encode=encode, elapsed_s=0)

    print(
        f"{'embed' if encode else 'chunk'} {section_id}: files={len(files)} "
        f"already={already} batch={batch_size if encode else '-'} out={out_dir}",
        file=sys.stderr,
    )
    for path in files:
        existing = states.get(path.name)
        if _is_done(existing, encode, out_dir):
            continue
        file_started = time.perf_counter()
        rows = list(_iter_passages(path, source, estimate_tokens))
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
        _write_checkpoint(
            out_dir,
            section_id=section_id,
            files=files,
            states=states,
            encode=encode,
            elapsed_s=elapsed,
        )
        rate = (len(rows) / file_s) if file_s > 0 else 0
        print(
            f"{path.name} passages={len(rows)} {rate:.0f}/s "
            f"done={sum(1 for item in files if _is_done(states.get(item.name), encode, out_dir))}/{len(files)}",
            file=sys.stderr,
        )

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
    tmp = path.with_suffix(path.suffix + ".tmp")
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
