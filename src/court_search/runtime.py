from __future__ import annotations

import os
from pathlib import Path

from court_search.config import AppConfig
from court_search.dictionaries import DictionarySet
from court_search.retrieval.service import SearchService
from court_search.storage.db import StateStore


def config_path() -> Path:
    raw = os.environ.get("COURT_SEARCH_CONFIG")
    if raw:
        return Path(raw).expanduser()
    return Path(__file__).resolve().parents[2] / "config" / "local.yaml"


def load_config(path: Path | None = None) -> AppConfig:
    return AppConfig.load(path or config_path())


def build_service(config: AppConfig | None = None) -> SearchService:
    config = config or load_config()
    for directory in (
        config.storage.normalized,
        config.storage.chunks,
        config.storage.embeddings,
        config.storage.reports,
        config.storage.snapshots,
        config.storage.secrets,
        config.storage.state_db.parent,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    store = StateStore(config.storage.state_db)
    dictionaries = DictionarySet(config.dictionaries_dir)
    return SearchService(config, store, dictionaries)
