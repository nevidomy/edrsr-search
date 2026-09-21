import pytest
from mcp.client import Client

from court_search.config import AppConfig
from court_search.ingest.pipeline import ingest_slice
from court_search.mcp.server import build_mcp_server
from court_search.runtime import build_service
from test_service import _write_config, _write_parquet


@pytest.mark.asyncio
async def test_mcp_search_and_read(tmp_path, monkeypatch):
    root = tmp_path / "raw"
    _write_parquet(root)
    config_path = _write_config(tmp_path, root)
    monkeypatch.setenv("COURT_SEARCH_CONFIG", str(config_path))
    config = AppConfig.load(config_path)
    service = build_service(config)
    ingest_slice(config, service.store, name="mcp-slice", limit=5)
    server = build_mcp_server(service)
    async with Client(server) as client:
        tools = await client.list_tools()
        assert {tool.name for tool in tools.tools} == {
            "search_decisions",
            "get_decision",
            "get_case_decisions",
            "lookup_filters",
            "get_corpus_info",
        }
        search = await client.call_tool("search_decisions", {"query": "поновлення строку", "limit": 1})
        assert search.is_error is False
        doc_id = search.structured_content["results"][0]["metadata"]["doc_id"]
        read = await client.call_tool("get_decision", {"doc_id": doc_id})
        assert read.is_error is False
        assert read.structured_content["paragraphs"]
