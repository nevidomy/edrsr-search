from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import yaml

from court_search.config import AppConfig
from court_search.embed.run import _is_done, _load_file_states, chunk_section


def _config(tmp_path: Path, parquet_root: Path) -> Path:
    data_root = tmp_path / "data"
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "raw_root": str(parquet_root),
                "sources": [
                    {
                        "repo": "overthelex/edrsr-tspk",
                        "path": str(parquet_root / "edrsr-tspk"),
                        "revision": None,
                        "jurisdiction": "civil",
                    }
                ],
                "storage": {"root": str(data_root)},
                "search": {"backend": "local_fts"},
                "api": {
                    "token_file": str(data_root / "secrets" / "api.token"),
                    "token_hashes_file": str(data_root / "secrets" / "api-token-hashes"),
                },
                "dictionaries_dir": str(Path("config/dictionaries").resolve()),
            }
        ),
        encoding="utf-8",
    )
    return path


def _write_parquet(root: Path, name: str, text: str) -> None:
    directory = root / "edrsr-tspk" / "data"
    directory.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            "doc_id": pa.array([1000000001], pa.int64()),
            "text": pa.array([text], pa.string()),
            "judgment_code": pa.array([2], pa.int32()),
        }
    )
    pq.write_table(table, directory / name)


def test_chunk_writes_json_state_and_skips_on_rerun(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    _write_parquet(raw, "edrsr-000-000.parquet", "Перший абзац.\n\nДругий абзац.")
    cfg = AppConfig.load(_config(tmp_path, raw))
    first = chunk_section(cfg, section_id="civil")
    section = Path(first["output"])
    assert (section / "checkpoint.json").is_file()
    assert (section / "spec.json").is_file()
    assert (section / "files" / "edrsr-000-000.json").is_file()
    assert (section / "shards" / "edrsr-000-000.parquet").is_file()
    checkpoint = json.loads((section / "checkpoint.json").read_text())
    assert checkpoint["files_done"] == 1
    assert checkpoint["completed_files"] == ["edrsr-000-000.parquet"]

    shard_mtime = (section / "shards" / "edrsr-000-000.parquet").stat().st_mtime_ns
    second = chunk_section(cfg, section_id="civil")
    assert second["files_done"] == 1
    assert (section / "shards" / "edrsr-000-000.parquet").stat().st_mtime_ns == shard_mtime


def test_missing_shard_is_not_treated_as_done(tmp_path: Path) -> None:
    out = tmp_path / "civil"
    (out / "files").mkdir(parents=True)
    (out / "files" / "edrsr-000-000.json").write_text(
        json.dumps(
            {
                "source_file": "edrsr-000-000.parquet",
                "shard": "shards/edrsr-000-000.parquet",
                "status": "done",
                "has_vectors": True,
                "passages": 1,
                "documents": 1,
            }
        ),
        encoding="utf-8",
    )
    states = _load_file_states(out)
    assert states["edrsr-000-000.parquet"]["status"] == "incomplete"
    assert _is_done(states["edrsr-000-000.parquet"], True, out) is False


def _write_docs(root: Path, name: str, count: int) -> None:
    directory = root / "edrsr-tspk" / "data"
    directory.mkdir(parents=True, exist_ok=True)
    base = int(name.split("-")[1]) * 1000 + int(name.split("-")[2].split(".")[0]) * 100
    table = pa.table(
        {
            "doc_id": pa.array([1000000000 + base + i for i in range(count)], pa.int64()),
            "text": pa.array([f"Рішення {i}.\n\nМотивувальна частина {i}." for i in range(count)], pa.string()),
            "judgment_code": pa.array([2] * count, pa.int32()),
        }
    )
    pq.write_table(table, directory / name)


def test_shard_files_partitions_deterministically_and_balances(tmp_path: Path) -> None:
    from court_search.embed.run import parse_shard, shard_files

    files = []
    for index, size in enumerate([900, 500, 400, 300, 200, 100, 100, 50]):
        path = tmp_path / f"edrsr-{index:03d}-000.parquet"
        path.write_bytes(b"x" * size)
        files.append(path)
    groups = [shard_files(files, k, 3) for k in range(3)]
    flat = [path for group in groups for path in group]
    assert sorted(flat) == sorted(files)
    assert len(flat) == len(set(flat))
    loads = [sum(path.stat().st_size for path in group) for group in groups]
    assert max(loads) - min(loads) <= 200
    assert groups == [shard_files(list(reversed(files)), k, 3) for k in range(3)]
    assert parse_shard("1/4") == (1, 4)
    assert parse_shard(None) is None
    for bad in ("4/4", "-1/2", "x", "1/0"):
        try:
            parse_shard(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_workers_share_section_without_duplicate_work(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    for index in range(5):
        _write_docs(raw, f"edrsr-{index:03d}-000.parquet", 3 + index)
    cfg = AppConfig.load(_config(tmp_path, raw))
    section = Path(cfg.storage.embeddings) / "bge-m3-dense-v1" / "civil"
    (section / "claims").mkdir(parents=True)
    # Another worker already holds the largest file: gpu0 must leave it alone.
    (section / "claims" / "edrsr-004-000.claim").write_text("gpu1")

    first = chunk_section(cfg, section_id="civil", worker="gpu0")
    assert first["files_done"] == 4
    assert not (section / "files" / "edrsr-004-000.json").exists()
    progress = json.loads((section / "workers" / "gpu0.json").read_text())
    assert progress["files_done"] == 4 and progress["current"] is None

    shard_mtime = (section / "shards" / "edrsr-000-000.parquet").stat().st_mtime_ns
    second = chunk_section(cfg, section_id="civil", worker="gpu1")
    assert second["files_done"] == 5
    assert json.loads((section / "workers" / "gpu1.json").read_text())["files_done"] == 1
    assert (section / "shards" / "edrsr-000-000.parquet").stat().st_mtime_ns == shard_mtime
    checkpoint = json.loads((section / "checkpoint.json").read_text())
    assert checkpoint["files_done"] == 5 and checkpoint["finished"] is True


def test_machine_shards_cover_section_once(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    for index in range(6):
        _write_docs(raw, f"edrsr-{index:03d}-000.parquet", 2 + index)
    cfg = AppConfig.load(_config(tmp_path, raw))
    reports = [chunk_section(cfg, section_id="civil", shard=(k, 2)) for k in range(2)]
    assert [report["files_total"] for report in reports] == [3, 3]
    section = Path(reports[0]["output"])
    assert len(list((section / "shards").glob("*.parquet"))) == 6
