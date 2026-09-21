from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from court_search.api.auth import load_token_hashes, require_token
from court_search.api.oauth import attach_oauth, mcp_www_authenticate, public_origin
from court_search.domain.contracts import (
    CaseDecisionsRequest,
    CaseDecisionsResponse,
    CorpusInfo,
    CorpusInfoRequest,
    FilterLookupRequest,
    FilterLookupResponse,
    ReadRequest,
    ReadResponse,
    SearchRequest,
    SearchResponse,
)
from court_search.domain.errors import DomainError
from court_search.mcp.http import attach_mcp
from court_search.runtime import build_service, load_config


def create_app() -> FastAPI:
    config = load_config()
    service = build_service(config)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with app.state.mcp_session_cm:
            yield

    app = FastAPI(
        title="Ukrainian court search",
        version="0.1.0",
        lifespan=lifespan,
        description="Read-only search and evidence API for EDRSR court decisions.",
    )
    app.state.service = service
    app.state.config = config
    app.state.token_hashes = load_token_hashes(config.api.token_hashes_file)
    attach_oauth(app)
    attach_mcp(app, service, config)

    @app.exception_handler(DomainError)
    async def domain_error(_request: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(exc.to_dict(), status_code=exc.http_status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            {
                "code": "INVALID_ARGUMENT",
                "message": "Request failed validation",
                "retryable": False,
                "details": {"errors": exc.errors()},
            },
            status_code=400,
        )

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        path = request.url.path
        public = (
            path.startswith("/health")
            or path.startswith("/.well-known/")
            or path.startswith("/oauth/")
            or path in {"/openapi.json", "/docs", "/redoc"}
        )
        if not public and (path.startswith("/api") or path.startswith("/mcp")):
            try:
                await require_token(request)
            except Exception as exc:  # HTTPException
                status = getattr(exc, "status_code", 401)
                detail = getattr(exc, "detail", "Unauthorized")
                headers = {}
                if status == 401 and path.startswith("/mcp"):
                    headers["WWW-Authenticate"] = mcp_www_authenticate(public_origin(request))
                return JSONResponse(
                    {"code": "UNAUTHORIZED", "message": str(detail), "retryable": False},
                    status_code=status,
                    headers=headers,
                )
        return await call_next(request)

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "live"}

    @app.get("/health/ready")
    async def ready() -> JSONResponse:
        info = service.get_corpus_info()
        if info.ready:
            return JSONResponse({"status": "ready", "generation": info.active_generation})
        return JSONResponse(
            {"status": "not_ready", "reason": "CORPUS_NOT_READY", "generation": info.active_generation},
            status_code=503,
        )

    @app.post("/api/v1/search", response_model=SearchResponse, dependencies=[Depends(require_token)])
    async def search(payload: SearchRequest) -> SearchResponse:
        return service.search(payload)

    @app.post("/api/v1/decisions/read", response_model=ReadResponse, dependencies=[Depends(require_token)])
    async def read_decision(payload: ReadRequest) -> ReadResponse:
        return service.get_decision(payload)

    @app.post("/api/v1/cases/decisions", response_model=CaseDecisionsResponse, dependencies=[Depends(require_token)])
    async def case_decisions(payload: CaseDecisionsRequest) -> CaseDecisionsResponse:
        return service.get_case_decisions(payload)

    @app.post("/api/v1/filters/lookup", response_model=FilterLookupResponse, dependencies=[Depends(require_token)])
    async def filters_lookup(payload: FilterLookupRequest) -> FilterLookupResponse:
        return service.lookup_filters(payload)

    @app.get("/api/v1/corpus", response_model=CorpusInfo, dependencies=[Depends(require_token)])
    async def corpus(corpus_generation: str | None = None) -> CorpusInfo:
        return service.get_corpus_info(CorpusInfoRequest(corpus_generation=corpus_generation))

    return app


app = create_app()
