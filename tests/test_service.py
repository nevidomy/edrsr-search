from __future__ import annotations

from pathlib import Path
from shutil import copytree

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import yaml

from court_search.api.auth import init_secrets
from court_search.config import AppConfig
from court_search.domain.contracts import ReadRequest, SearchRequest
from court_search.domain.errors import CorpusNotReady
from court_search.ingest.pipeline import ingest_slice
from court_search.runtime import build_service
from court_search.text.spans import validate_quote


def _copy_dictionaries(tmp_path: Path) -> Path:
    dest = tmp_path / "dictionaries"
    copytree(Path("config/dictionaries").resolve(), dest)
    return dest


def _write_config(tmp_path: Path, parquet_root: Path) -> Path:
    data_root = tmp_path / "data"
    config_path = tmp_path / "config.yaml"
    payload = {
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
        "dictionaries_dir": str(_copy_dictionaries(tmp_path)),
    }
    config_path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return config_path


def _write_parquet(root: Path) -> None:
    directory = root / "edrsr-tspk" / "data"
    directory.mkdir(parents=True)
    table = pa.table(
        {
            "doc_id": pa.array([1000000001], pa.int64()),
            "cause_num": pa.array(["2-85/05"], pa.string()),
            "court_code": pa.array(["617"], pa.string()),
            "judge": pa.array(["Тестовий С. С."], pa.string()),
            "adjudication_date": pa.array(["2005-01-13 22:00:00+00"], pa.string()),
            "judgment_code": pa.array([3], pa.int32()),
            "category_code": pa.array([1060], pa.int32()),
            "year": pa.array([2005], pa.int16()),
            "justice_kind": pa.array([1], pa.int8()),
            "justice_kind_name": pa.array(["civil"], pa.string()),
            "text": pa.array(["Приклад тексту судового акта про поновлення строку."], pa.string()),
        }
    )
    pq.write_table(table, directory / "edrsr-000-000.parquet")


def test_corpus_not_ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "raw"
    root.mkdir()
    config_path = _write_config(tmp_path, root)
    monkeypatch.setenv("COURT_SEARCH_CONFIG", str(config_path))
    service = build_service(AppConfig.load(config_path))
    info = service.get_corpus_info()
    assert info.status == "CORPUS_NOT_READY"
    assert info.ready is False
    with pytest.raises(CorpusNotReady):
        service.search(SearchRequest(query="поновлення строку"))


def test_slice_search_and_citation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "raw"
    _write_parquet(root)
    config_path = _write_config(tmp_path, root)
    monkeypatch.setenv("COURT_SEARCH_CONFIG", str(config_path))
    config = AppConfig.load(config_path)
    service = build_service(config)
    init_secrets(config.api)
    ingest_slice(config, service.store, name="test-slice", limit=10)
    info = service.get_corpus_info()
    assert info.ready is True
    result = service.search(SearchRequest(query="поновлення строку", limit=5, rerank=False))
    assert result.returned_count == 1
    hit = result.results[0]
    record = service.store.get_decision(result.corpus_generation, hit.metadata.doc_id)
    assert record is not None
    evidence = hit.evidence[0]
    validate_quote(record.text, evidence.char_start, evidence.char_end, evidence.quote)
    read = service.get_decision(ReadRequest(doc_id=hit.metadata.doc_id))
    assert read.paragraphs
    assert read.paragraphs[0].text.startswith("Приклад")
