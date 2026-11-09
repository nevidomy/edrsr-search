#!/usr/bin/env python3
"""Write a rental-only court-search YAML. No API tokens, no OpenSearch."""
from __future__ import annotations

import os
from pathlib import Path

from court_search.embed.sections import SECTION_BY_ID

SECTIONS = (os.environ.get("SECTIONS") or os.environ.get("SECTION") or "civil").split()
DATA_ROOT = Path(os.environ.get("DATA_ROOT", "/workspace/edrsr"))
OUT = Path(os.environ.get("COURT_SEARCH_CONFIG", DATA_ROOT / "work" / "rental.yaml"))

specs = [SECTION_BY_ID[name] for name in SECTIONS]
work = DATA_ROOT / "work"
secrets = work / "secrets"
secrets.mkdir(parents=True, exist_ok=True)
(secrets / "api.token").touch()
(secrets / "api-token-hashes").touch()
OUT.parent.mkdir(parents=True, exist_ok=True)

repo_root = Path(__file__).resolve().parents[3]
dicts = repo_root / "config" / "dictionaries"

sources = "".join(
    f"""  - repo: {spec["repo"]}
    path: {DATA_ROOT / spec["folder"]}
    revision: null
    jurisdiction: {spec["jurisdiction"].value}
"""
    for spec in specs
)

OUT.write_text(
    f"""raw_root: {DATA_ROOT}

sources:
{sources}
storage:
  root: {work}
  embeddings: {DATA_ROOT / "embeddings"}
  models: {DATA_ROOT / "models"}
  archive: {DATA_ROOT / "archive"}

search:
  url: http://127.0.0.1:9200
  backend: local_fts

api:
  token_file: {secrets / "api.token"}
  token_hashes_file: {secrets / "api-token-hashes"}

dictionaries_dir: {dicts}
""",
    encoding="utf-8",
)
print(OUT)
