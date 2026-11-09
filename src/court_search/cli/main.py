from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

import httpx
import typer
import uvicorn

from court_search import API_SCHEMA_VERSION, TOOL_SCHEMA_VERSION
from court_search.api.auth import init_secrets
from court_search.domain.contracts import (
    CaseDecisionsRequest,
    FilterLookupRequest,
    ReadRequest,
    SearchRequest,
)
from court_search.domain.enums import Jurisdiction
from court_search.ingest.full import ingest_full, restore_archive
from court_search.ingest.pipeline import ingest_corpus, ingest_slice
from court_search.runtime import build_service, config_path, load_config
from court_search.text.spans import validate_quote

app = typer.Typer(no_args_is_help=True, add_completion=False)


def _opensearch_doctor(cfg) -> dict:
    if cfg.search.backend != "opensearch":
        return {"configured": False}
    try:
        from court_search.indexing.opensearch import OpenSearchIndex

        index = OpenSearchIndex(cfg.search)
        return {
            "configured": True,
            "reachable": index.ping(),
            "info": index.info().get("version", {}),
            "plugins": index.plugins(),
        }
    except Exception as exc:  # noqa: BLE001
        return {"configured": True, "reachable": False, "error": str(exc)}


def _config(config: Optional[Path]) -> Path:
    return config or config_path()


@app.command("init-secrets")
def init_secrets_cmd(config: Optional[Path] = typer.Option(None, "--config")) -> None:
    cfg = load_config(_config(config))
    token = init_secrets(cfg.api)
    typer.echo(f"token file: {cfg.api.token_file}")
    typer.echo(f"hash file: {cfg.api.token_hashes_file}")
    typer.echo(f"token: {token}")


@app.command("doctor")
def doctor(config: Optional[Path] = typer.Option(None, "--config")) -> None:
    cfg = load_config(_config(config))
    service = build_service(cfg)
    info = service.get_corpus_info()
    report = {
        "config": str(_config(config)),
        "state_db": str(cfg.storage.state_db),
        "sources": [
            {
                "repo": source.repo,
                "path": str(source.path),
                "exists": source.path.exists(),
                "parquet": len(list((source.path / "data").glob("*.parquet"))) if (source.path / "data").is_dir() else 0,
            }
            for source in cfg.sources
        ],
        "corpus": info.model_dump(mode="json"),
        "token_hashes": cfg.api.token_hashes_file.exists(),
        "search_backend": cfg.search.backend,
        "opensearch": _opensearch_doctor(cfg),
    }
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2, default=str))


@app.command("sources")
def sources_cmd(
    action: str = typer.Argument("inspect"),
    config: Optional[Path] = typer.Option(None, "--config"),
) -> None:
    if action != "inspect":
        raise typer.BadParameter("only 'inspect' is implemented")
    doctor(config)


@app.command("ingest-slice")
def ingest_slice_cmd(
    config: Optional[Path] = typer.Option(None, "--config"),
    limit: int = typer.Option(400, "--limit"),
    name: str = typer.Option("slice-v1", "--name"),
) -> None:
    cfg = load_config(_config(config))
    service = build_service(cfg)
    report = ingest_slice(cfg, service.store, name=name, limit=limit)
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2, default=str))


@app.command("ingest-pilot")
def ingest_pilot_cmd(
    config: Optional[Path] = typer.Option(None, "--config"),
    limit: int = typer.Option(200_000, "--limit"),
    name: str = typer.Option("pilot-v1", "--name"),
    activate: bool = typer.Option(False, "--activate"),
) -> None:
    cfg = load_config(_config(config))
    service = build_service(cfg)
    report = ingest_corpus(
        cfg,
        service.store,
        name=name,
        limit=limit,
        strategy="stratified",
        activate=activate,
        rebuild_fts=False,
    )
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2, default=str))


@app.command("ingest-full")
def ingest_full_cmd(
    config: Optional[Path] = typer.Option(None, "--config"),
    name: str = typer.Option("full-v1", "--name"),
    jurisdiction: Optional[list[Jurisdiction]] = typer.Option(
        None,
        "--jurisdiction",
        help="Repeat to limit jurisdictions. Default: all five.",
    ),
    activate: bool = typer.Option(False, "--activate"),
    shards: int = typer.Option(2, "--shards"),
    chunk_gib: int = typer.Option(80, "--chunk-gib"),
    threads: int = typer.Option(12, "--threads"),
    bulk_size: int = typer.Option(400, "--bulk-size"),
) -> None:
    from court_search.ingest.full import ALL_JURISDICTIONS

    cfg = load_config(_config(config))
    service = build_service(cfg)
    report = ingest_full(
        cfg,
        service.store,
        name=name,
        jurisdictions=list(jurisdiction or ALL_JURISDICTIONS),
        activate=activate,
        shards=shards,
        chunk_gib=chunk_gib,
        thread_count=threads,
        bulk_size=bulk_size,
    )
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2, default=str))


@app.command("restore-archive")
def restore_archive_cmd(
    config: Optional[Path] = typer.Option(None, "--config"),
    name: str = typer.Option("civil-v1", "--name"),
    activate: bool = typer.Option(False, "--activate"),
) -> None:
    cfg = load_config(_config(config))
    service = build_service(cfg)
    report = restore_archive(cfg, service.store, name=name, activate=activate)
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2, default=str))


@app.command("embed-sections")
def embed_sections_cmd() -> None:
    from court_search.embed.run import list_sections

    typer.echo(json.dumps(list_sections(), ensure_ascii=False, indent=2))


@app.command("chunk-passages")
def chunk_passages_cmd(
    section: str = typer.Option(..., "--section", help="civil | administrative | criminal | commercial | administrative_offense"),
    config: Optional[Path] = typer.Option(None, "--config"),
    limit_files: Optional[int] = typer.Option(None, "--limit-files"),
    spec_name: str = typer.Option("bge-m3-dense-v1", "--spec-name"),
) -> None:
    from court_search.embed.run import chunk_section

    report = chunk_section(
        load_config(_config(config)),
        section_id=section,
        limit_files=limit_files,
        spec_name=spec_name,
    )
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("embed-passages")
def embed_passages_cmd(
    section: str = typer.Option(..., "--section"),
    config: Optional[Path] = typer.Option(None, "--config"),
    batch_size: int = typer.Option(16, "--batch-size", help="16 on 3060; try 64–128 on 5090"),
    max_length: int = typer.Option(1000, "--max-length"),
    device: Optional[str] = typer.Option(None, "--device"),
    limit_files: Optional[int] = typer.Option(None, "--limit-files"),
    min_source_rows: Optional[int] = typer.Option(None, "--min-source-rows"),
    max_passages: Optional[int] = typer.Option(None, "--max-passages"),
    spec_name: str = typer.Option("bge-m3-dense-v1", "--spec-name"),
    shard: Optional[str] = typer.Option(
        None, "--shard", help="k/n: this machine's size-balanced share of files (0-based k)"
    ),
    worker: Optional[str] = typer.Option(
        None, "--worker", help="Name for one of several processes sharing the output dir (one per GPU)"
    ),
) -> None:
    from court_search.embed.run import embed_section, parse_shard

    report = embed_section(
        load_config(_config(config)),
        section_id=section,
        batch_size=batch_size,
        max_length=max_length,
        device=device,
        limit_files=limit_files,
        min_source_rows=min_source_rows,
        max_passages=max_passages,
        spec_name=spec_name,
        shard=parse_shard(shard),
        worker=worker,
    )
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("index-passages")
def index_passages_cmd(
    section: str = typer.Option(..., "--section"),
    config: Optional[Path] = typer.Option(None, "--config"),
    limit_files: Optional[int] = typer.Option(None, "--limit-files", help="Max new files; omit for all remaining"),
    min_passages: int = typer.Option(0, "--min-passages"),
    recreate: bool = typer.Option(False, "--recreate"),
    resume: bool = typer.Option(True, "--resume/--no-resume"),
    spec_name: str = typer.Option("bge-m3-dense-v1", "--spec-name"),
    shard: Optional[str] = typer.Option(
        None, "--shard", help="k/n: load the k-th of n size-balanced file groups (run n loaders in parallel)"
    ),
    from_dir: Optional[Path] = typer.Option(
        None, "--from-dir", help="index plain parquet files from this dir (e.g. the HF dataset's data/<section>)"
    ),
) -> None:
    from court_search.embed.index_os import index_passage_shards
    from court_search.embed.run import parse_shard

    report = index_passage_shards(
        load_config(_config(config)),
        section_id=section,
        spec_name=spec_name,
        limit_files=limit_files,
        min_passages=min_passages,
        recreate=recreate,
        resume=resume,
        shard=parse_shard(shard),
        from_dir=from_dir,
    )
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("activate")
def activate_cmd(
    generation: str = typer.Argument(...),
    config: Optional[Path] = typer.Option(None, "--config"),
) -> None:
    service = build_service(load_config(_config(config)))
    service.store.activate(generation)
    typer.echo(json.dumps({"active": generation}, indent=2))


@app.command("index-lexical")
def index_lexical(
    config: Optional[Path] = typer.Option(None, "--config"),
    generation: Optional[str] = typer.Option(None, "--generation"),
) -> None:
    from court_search.indexing.opensearch import OpenSearchIndex

    cfg = load_config(_config(config))
    service = build_service(cfg)
    name = generation or service.store.active_generation()
    if not name:
        raise typer.BadParameter("no generation to index")
    index = OpenSearchIndex(cfg.search)
    created = index.recreate(name)
    report = index.bulk_index(name, service.store.iter_decisions(name))
    report["created"] = created
    analyze = {
        "ukrainian": index.analyze("ukrainian", "ухвали", index=created),
        "plain": index.analyze("ukrainian_plain", "ухвали", index=created),
    }
    report["analyze"] = analyze
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("serve")
def serve(
    config: Optional[Path] = typer.Option(None, "--config"),
    host: Optional[str] = typer.Option(None, "--host"),
    port: Optional[int] = typer.Option(None, "--port"),
) -> None:
    cfg = load_config(_config(config))
    if not cfg.api.token_hashes_file.exists():
        init_secrets(cfg.api)
    os.environ["COURT_SEARCH_CONFIG"] = str(_config(config))
    uvicorn.run(
        "court_search.api.app:app",
        host=host or cfg.api.host,
        port=port or cfg.api.internal_port,
        reload=False,
    )


@app.command("export-contracts")
def export_contracts(
    output: Path = typer.Option(Path("contracts"), "--output"),
    config: Optional[Path] = typer.Option(None, "--config"),
) -> None:
    from court_search.adapters.openai import tool_schemas
    from court_search.api.app import create_app

    os.environ.setdefault("COURT_SEARCH_CONFIG", str(_config(config)))
    application = create_app()
    output.mkdir(parents=True, exist_ok=True)
    (output / "openapi.json").write_text(
        json.dumps(application.openapi(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output / "openai-tools.json").write_text(
        json.dumps(tool_schemas(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output / "versions.json").write_text(
        json.dumps(
            {"api_schema_version": API_SCHEMA_VERSION, "tool_schema_version": TOOL_SCHEMA_VERSION},
            indent=2,
        ),
        encoding="utf-8",
    )
    typer.echo(f"wrote {output}")


@app.command("smoke-test")
def smoke_test(
    api_url: str = typer.Option("http://127.0.0.1:8000", "--api-url"),
    token: Optional[str] = typer.Option(None, "--token"),
    query: str = typer.Option("поновлення строку", "--query"),
) -> None:
    secret = token or os.environ.get("COURT_SEARCH_TOKEN")
    if not secret:
        raise typer.BadParameter("set COURT_SEARCH_TOKEN or pass --token")
    client = httpx.Client(
        base_url=api_url.rstrip("/"),
        headers={"Authorization": f"Bearer {secret}"},
        timeout=30.0,
    )
    live = client.get("/health/live")
    live.raise_for_status()
    corpus = client.get("/api/v1/corpus")
    corpus.raise_for_status()
    info = corpus.json()
    if not info.get("ready"):
        typer.echo(json.dumps(info, ensure_ascii=False, indent=2))
        raise typer.Exit(code=2)
    search = client.post("/api/v1/search", json=SearchRequest(query=query, limit=3).model_dump(mode="json"))
    search.raise_for_status()
    body = search.json()
    if not body["results"]:
        raise typer.Exit(code=3)
    hit = body["results"][0]
    evidence = hit["evidence"][0]
    read = client.post(
        "/api/v1/decisions/read",
        json=ReadRequest(doc_id=hit["metadata"]["doc_id"]).model_dump(mode="json"),
    )
    read.raise_for_status()
    service = build_service()
    record = service._get_record(body["corpus_generation"], hit["metadata"]["doc_id"])
    if record is None:
        raise typer.Exit(code=4)
    validate_quote(record.text, evidence["char_start"], evidence["char_end"], evidence["quote"])
    case_number = hit["metadata"].get("case_number")
    if case_number:
        cases = client.post(
            "/api/v1/cases/decisions",
            json=CaseDecisionsRequest(case_number=case_number, limit=5).model_dump(mode="json"),
        )
        cases.raise_for_status()
    filters = client.post(
        "/api/v1/filters/lookup",
        json=FilterLookupRequest(field="jurisdiction").model_dump(mode="json"),
    )
    filters.raise_for_status()
    typer.echo(
        json.dumps(
            {
                "generation": info["active_generation"],
                "search_doc_id": hit["metadata"]["doc_id"],
                "citation_id": evidence["citation_id"],
                "quote_matches_source": True,
                "registry_url": hit["metadata"]["registry_url"],
                "read_paragraphs": len(read.json()["paragraphs"]),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
