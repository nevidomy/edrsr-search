from __future__ import annotations

import argparse
import os
import sys
from typing import Any

import httpx
from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent

from court_search.domain.contracts import SearchFilters
from court_search.domain.enums import FilterField, Jurisdiction, SearchMode
from court_search.mcp.server import INSTRUCTIONS, READONLY


def _client(api_url: str) -> httpx.Client:
    token = os.environ.get("COURT_SEARCH_TOKEN")
    if not token:
        print("COURT_SEARCH_TOKEN is not set", file=sys.stderr)
        raise SystemExit(2)
    return httpx.Client(
        base_url=api_url.rstrip("/"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=35.0,
    )


def _call(client: httpx.Client, method: str, path: str, payload: dict[str, Any] | None = None) -> CallToolResult:
    response = client.request(method, path, json=payload)
    try:
        data = response.json()
    except ValueError:
        data = {"code": "UPSTREAM_ERROR", "message": response.text[:500], "retryable": True}
    if response.is_error:
        message = data.get("message") if isinstance(data, dict) else str(data)
        code = data.get("code") if isinstance(data, dict) else "UPSTREAM_ERROR"
        return CallToolResult(
            content=[TextContent(type="text", text=f"{code}: {message}")],
            structured_content=data if isinstance(data, dict) else {"error": data},
            is_error=True,
        )
    return CallToolResult(
        content=[TextContent(type="text", text=_compact(data))],
        structured_content=data,
    )


def _compact(data: Any) -> str:
    if not isinstance(data, dict):
        return str(data)
    if "results" in data and "returned_count" in data:
        return f"returned={data.get('returned_count')} generation={data.get('corpus_generation')}"
    if "status" in data and "ready" in data:
        return f"status={data.get('status')} ready={data.get('ready')} generation={data.get('active_generation')}"
    if "paragraphs" in data:
        return f"doc_id={data.get('metadata', {}).get('doc_id')} paragraphs={len(data.get('paragraphs', []))}"
    return "ok"


def build_bridge(api_url: str) -> MCPServer:
    client = _client(api_url)
    mcp = MCPServer("ukrainian-court-search", instructions=INSTRUCTIONS)

    @mcp.tool(name="search_decisions", annotations=READONLY)
    def search_decisions(
        query: str,
        mode: SearchMode = SearchMode.HYBRID,
        filters: SearchFilters | None = None,
        required_phrases: list[str] | None = None,
        limit: int = 10,
        rerank: bool = True,
        allow_fallback: bool = True,
        max_response_chars: int = 30_000,
        corpus_generation: str | None = None,
        cursor: str | None = None,
    ) -> CallToolResult:
        return _call(
            client,
            "POST",
            "/api/v1/search",
            {
                "query": query,
                "mode": mode,
                "filters": (filters or SearchFilters()).model_dump(mode="json"),
                "required_phrases": required_phrases or [],
                "limit": limit,
                "rerank": rerank,
                "allow_fallback": allow_fallback,
                "max_response_chars": max_response_chars,
                "corpus_generation": corpus_generation,
                "cursor": cursor,
            },
        )

    @mcp.tool(name="get_decision", annotations=READONLY)
    def get_decision(
        doc_id: str | None = None,
        document_version: str | None = None,
        corpus_generation: str | None = None,
        passage_id: str | None = None,
        paragraph_start: str | None = None,
        paragraph_end: str | None = None,
        max_response_chars: int = 30_000,
        cursor: str | None = None,
    ) -> CallToolResult:
        return _call(
            client,
            "POST",
            "/api/v1/decisions/read",
            {
                "doc_id": doc_id,
                "document_version": document_version,
                "corpus_generation": corpus_generation,
                "passage_id": passage_id,
                "paragraph_start": paragraph_start,
                "paragraph_end": paragraph_end,
                "max_response_chars": max_response_chars,
                "cursor": cursor,
            },
        )

    @mcp.tool(name="get_case_decisions", annotations=READONLY)
    def get_case_decisions(
        case_number: str,
        court_codes: list[str] | None = None,
        jurisdictions: list[Jurisdiction] | None = None,
        limit: int = 50,
        corpus_generation: str | None = None,
        cursor: str | None = None,
    ) -> CallToolResult:
        return _call(
            client,
            "POST",
            "/api/v1/cases/decisions",
            {
                "case_number": case_number,
                "court_codes": court_codes,
                "jurisdictions": jurisdictions,
                "limit": limit,
                "corpus_generation": corpus_generation,
                "cursor": cursor,
            },
        )

    @mcp.tool(name="lookup_filters", annotations=READONLY)
    def lookup_filters(
        field: FilterField,
        query: str | None = None,
        jurisdiction: Jurisdiction | None = None,
        limit: int = 20,
    ) -> CallToolResult:
        return _call(
            client,
            "POST",
            "/api/v1/filters/lookup",
            {"field": field, "query": query, "jurisdiction": jurisdiction, "limit": limit},
        )

    @mcp.tool(name="get_corpus_info", annotations=READONLY)
    def get_corpus_info(corpus_generation: str | None = None) -> CallToolResult:
        path = "/api/v1/corpus"
        if corpus_generation:
            path += f"?corpus_generation={corpus_generation}"
        return _call(client, "GET", path)

    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(description="Stdio MCP bridge for court-search REST")
    parser.add_argument("--api-url", required=True, help="Backend origin, e.g. http://127.0.0.1:8000")
    parser.add_argument("--config", default=None, help="Optional non-secret configuration path")
    args = parser.parse_args()
    if args.config:
        print(f"using config {args.config}", file=sys.stderr)
    mcp = build_bridge(args.api_url)
    mcp.run()


if __name__ == "__main__":
    main()
