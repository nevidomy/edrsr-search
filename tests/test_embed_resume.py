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
