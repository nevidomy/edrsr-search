from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from court_search.embed import index_os


class _FakeIndex:
    loaded: list[str] = []

    def __init__(self, _config) -> None:
        class _Indices:
            def refresh(self, index: str) -> None:
                pass

        self.client = type("C", (), {"indices": _Indices()})()

    def ping(self) -> bool:
        return True

    def ensure_passage_index(self, *, spec_name: str, recreate: bool) -> str:
        return f"passages-{spec_name}"

    def bulk_passages(self, name, actions):
        docs = list(actions)
        _FakeIndex.loaded += [doc["passage_id"] for doc in docs]
        return len(docs), 0

    def passage_count(self, spec_name: str) -> int:
        return len(_FakeIndex.loaded)


def _shard(path: Path, stem: str, rows: int) -> None:
    vec = np.ones(1024, dtype=np.float16).tobytes()
    table = pa.table({
        "passage_id": [f"{stem}:{i}" for i in range(rows)],
        "doc_id": [str(i) for i in range(rows)],
        "jurisdiction": ["civil"] * rows,
        "char_start": [0] * rows,
        "char_end": [10] * rows,
        "text": ["x" * 50] * rows,
        "vector": [vec] * rows,
    })
    pq.write_table(table, path / f"{stem}.parquet")


def test_parallel_loaders_cover_each_file_once_and_skip_indexed(tmp_path: Path, monkeypatch) -> None:
    section = tmp_path / "embeddings" / "bge-m3-dense-v1" / "civil"
    (section / "shards").mkdir(parents=True)
    for i, rows in enumerate([5, 3, 8, 1, 4, 6]):
        _shard(section / "shards", f"edrsr-{i:03d}-000", rows)
    (section / "os-index.json").write_text(json.dumps({"files": ["edrsr-002-000.parquet"], "indexed": 8}))
    monkeypatch.setattr(index_os, "OpenSearchIndex", _FakeIndex)
    monkeypatch.setattr(index_os, "_shard_root", lambda config, section_id, spec_name: section)
    _FakeIndex.loaded = []
    config = type("Cfg", (), {"search": None})()
    reports = [index_os.index_passage_shards(config, section_id="civil", shard=(k, 2)) for k in range(2)]
    files = [f for report in reports for f in report["files"]]
    assert sorted(files) == sorted(f"edrsr-{i:03d}-000.parquet" for i in (0, 1, 3, 4, 5))
    assert len(_FakeIndex.loaded) == len(set(_FakeIndex.loaded)) == 5 + 3 + 1 + 4 + 6
    assert (section / "os-index-0of2.json").is_file() and (section / "os-index-1of2.json").is_file()
    # Re-running resumes: nothing is sent twice.
    again = [index_os.index_passage_shards(config, section_id="civil", shard=(k, 2)) for k in range(2)]
    assert sum(r["indexed_this_run"] for r in again) == 0


def test_merge_then_resplit_does_not_resend(tmp_path: Path, monkeypatch) -> None:
    section = tmp_path / "embeddings" / "bge-m3-dense-v1" / "civil"
    (section / "shards").mkdir(parents=True)
    for i, rows in enumerate([5, 3, 8, 1, 4, 6, 2, 7]):
        _shard(section / "shards", f"edrsr-{i:03d}-000", rows)
    monkeypatch.setattr(index_os, "OpenSearchIndex", _FakeIndex)
    monkeypatch.setattr(index_os, "_shard_root", lambda config, section_id, spec_name: section)
    _FakeIndex.loaded = []
    config = type("Cfg", (), {"search": None})()
    index_os.index_passage_shards(config, section_id="civil", shard=(0, 2), limit_files=2)
    first = len(_FakeIndex.loaded)
    summary = index_os.merge_loader_reports(config, section_id="civil")
    assert summary["files"] == 2 and summary["merged_parts"] == 1
    for k in range(3):
        index_os.index_passage_shards(config, section_id="civil", shard=(k, 3))
    assert len(_FakeIndex.loaded) == len(set(_FakeIndex.loaded)) == 36
    assert first < 36


def test_from_dir_reads_published_float32_format(tmp_path: Path, monkeypatch) -> None:
    published = tmp_path / "hf" / "data" / "commercial"
    published.mkdir(parents=True)
    vec = pa.FixedSizeListArray.from_arrays(pa.array(np.full(3 * 1024, 0.03125, dtype=np.float32)), 1024)
    pq.write_table(pa.table({
        "passage_id": ["a:0", "a:1", "a:2"], "doc_id": ["1", "1", "2"], "jurisdiction": ["commercial"] * 3,
        "char_start": [0, 10, 0], "char_end": [10, 20, 5], "text": ["x"] * 3, "vector": vec,
    }), published / "edrsr-000-000.parquet")
    monkeypatch.setattr(index_os, "OpenSearchIndex", _FakeIndex)
    sent = []
    monkeypatch.setattr(_FakeIndex, "bulk_passages", lambda self, name, actions: (sent.extend(actions), (len(sent), 0))[1])
    report = index_os.index_passage_shards(type("Cfg", (), {"search": None})(), section_id="commercial", from_dir=published)
    assert report["indexed"] == 3 and len(sent) == 3
    assert len(sent[0]["vector"]) == 1024 and abs(sent[0]["vector"][0] - 0.03125) < 1e-9
    assert (published / "os-index.json").is_file()
