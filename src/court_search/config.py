from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from court_search.domain.enums import SOURCE_REPO_BY_JURISDICTION, Jurisdiction


class SourceConfig(BaseModel):
    repo: str
    path: Path
    revision: str | None = None
    jurisdiction: Jurisdiction | None = None


class StorageConfig(BaseModel):
    root: Path
    normalized: Path
    chunks: Path
    embeddings: Path
    state_db: Path
    reports: Path
    snapshots: Path
    secrets: Path
    models: Path
    archive: Path


class SearchTuning(BaseModel):
    url: str = "https://opensearch:9200"
    lexical_candidates: int = 100
    vector_passage_candidates: int = 300
    fusion_k: int = 60
    rerank_decisions: int = 50
    passages_per_decision: int = 2
    request_timeout_seconds: int = 30
    backend: str = "local_fts"
    snapshot_repo: str = "edrsr_fs"
    snapshot_container_path: str = "/usr/share/opensearch/snapshots"


class ApiConfig(BaseModel):
    host: str = "127.0.0.1"
    internal_port: int = 8000
    auth_profile: str = "private_token"
    token_file: Path
    token_hashes_file: Path
    default_response_chars: int = 30_000
    allowed_hosts: list[str] = Field(
        default_factory=lambda: ["127.0.0.1", "localhost", "localhost:8000", "127.0.0.1:8000"]
    )
    public_origin: str = "http://127.0.0.1:8000"


class AppConfig(BaseModel):
    sources: list[SourceConfig]
    storage: StorageConfig
    search: SearchTuning
    api: ApiConfig
    dictionaries_dir: Path
    raw_root: Path

    @classmethod
    def load(cls, path: Path) -> AppConfig:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls.model_validate(_resolve_paths(data, path.parent))


def _resolve_paths(data: dict[str, Any], config_dir: Path) -> dict[str, Any]:
    # "./x" is relative to the repository root (the parent of config/), not the CWD,
    # so services and CLI runs from anywhere agree on where data lives.
    base = config_dir.parent

    def resolve(value: Any) -> Any:
        if isinstance(value, str) and value.startswith("./"):
            return (base / value[2:]).resolve()
        if isinstance(value, str) and (value.startswith("/") or value.startswith("~")):
            return Path(value).expanduser()
        if isinstance(value, dict):
            return {k: resolve(v) for k, v in value.items()}
        if isinstance(value, list):
            return [resolve(v) for v in value]
        return value

    resolved = resolve(data)
    storage = resolved["storage"]
    root = Path(storage["root"])
    storage.setdefault("normalized", root / "normalized")
    storage.setdefault("chunks", root / "chunks")
    storage.setdefault("embeddings", root / "embeddings")
    storage.setdefault("state_db", root / "state" / "court-search.sqlite")
    storage.setdefault("reports", root / "reports")
    storage.setdefault("snapshots", root / "snapshots")
    storage.setdefault("secrets", root / "secrets")
    storage.setdefault("models", root / "models")
    storage.setdefault("archive", root / "archive")
    resolved["dictionaries_dir"] = Path(resolved.get("dictionaries_dir", config_dir / "dictionaries"))
    return resolved


def default_sources(raw_root: Path) -> list[dict[str, Any]]:
    mapping = {
        "edrsr-tspk": Jurisdiction.CIVIL,
        "edrsr-kas": Jurisdiction.ADMINISTRATIVE,
        "edrsr-kpk": Jurisdiction.CRIMINAL,
        "edrsr-gpk": Jurisdiction.COMMERCIAL,
        "edrsr-kupap": Jurisdiction.ADMINISTRATIVE_OFFENSE,
    }
    sources = []
    for folder, jurisdiction in mapping.items():
        sources.append(
            {
                "repo": SOURCE_REPO_BY_JURISDICTION[jurisdiction],
                "path": str(raw_root / folder),
                "revision": None,
                "jurisdiction": jurisdiction.value,
            }
        )
    return sources
