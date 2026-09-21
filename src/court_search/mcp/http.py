from __future__ import annotations

from fastapi import FastAPI
from mcp.server.transport_security import TransportSecuritySettings

from court_search.config import AppConfig
from court_search.mcp.server import build_mcp_server
from court_search.retrieval.service import SearchService


def attach_mcp(app: FastAPI, service: SearchService, config: AppConfig) -> None:
    mcp = build_mcp_server(service)
    security = TransportSecuritySettings(
        allowed_hosts=list(config.api.allowed_hosts),
        enable_dns_rebinding_protection=True,
    )
    # Mount at /mcp with path "/" so the public endpoint is /mcp, not /mcp/mcp.
    asgi = mcp.streamable_http_app(streamable_http_path="/", transport_security=security)
    app.mount("/mcp", asgi)
    app.state.mcp = mcp
    app.state.mcp_session_cm = mcp.session_manager.run()
