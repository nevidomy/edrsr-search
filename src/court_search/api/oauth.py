from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Any
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from court_search.api.auth import token_valid

router = APIRouter()


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def pkce_challenge(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


@dataclass
class OAuthStore:
    clients: dict[str, dict[str, Any]] = field(default_factory=dict)
    codes: dict[str, dict[str, Any]] = field(default_factory=dict)
    refresh: dict[str, str] = field(default_factory=dict)
    lock: Lock = field(default_factory=Lock)


def public_origin(request: Request) -> str:
    configured = getattr(request.app.state.config.api, "public_origin", "") or ""
    return configured.rstrip("/")


def mcp_resource(origin: str) -> str:
    return f"{origin}/mcp"


def attach_oauth(app) -> None:
    app.state.oauth = OAuthStore()
    app.include_router(router)


@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/mcp")
async def protected_resource(request: Request) -> dict[str, Any]:
    origin = public_origin(request)
    return {
        "resource": mcp_resource(origin),
        "authorization_servers": [origin],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["court-search"],
    }


@router.get("/.well-known/oauth-authorization-server")
async def authorization_server(request: Request) -> dict[str, Any]:
    origin = public_origin(request)
    return {
        "issuer": origin,
        "authorization_endpoint": f"{origin}/oauth/authorize",
        "token_endpoint": f"{origin}/oauth/token",
        "registration_endpoint": f"{origin}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
        "scopes_supported": ["court-search"],
    }


@router.post("/oauth/register")
async def register(request: Request) -> JSONResponse:
    body = await request.json()
    redirect_uris = body.get("redirect_uris") or []
    if not redirect_uris:
        raise HTTPException(status_code=400, detail="redirect_uris is required")
    client_id = secrets.token_urlsafe(16)
    record = {
        "client_id": client_id,
        "redirect_uris": list(redirect_uris),
        "token_endpoint_auth_method": "none",
        "client_id_issued_at": int(time.time()),
    }
    store: OAuthStore = request.app.state.oauth
    with store.lock:
        store.clients[client_id] = record
    return JSONResponse({**record, "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]})


@router.get("/oauth/authorize")
async def authorize_form(
    request: Request,
    client_id: str,
    redirect_uri: str,
    response_type: str = "code",
    state: str = "",
    code_challenge: str = "",
    code_challenge_method: str = "S256",
    scope: str = "",
) -> HTMLResponse:
    _validate_authorize(request, client_id, redirect_uri, response_type, code_challenge, code_challenge_method)
    html = f"""<!doctype html>
<html lang="uk"><head><meta charset="utf-8"><title>Court search — Claude</title>
<style>
body {{ font-family: sans-serif; max-width: 32rem; margin: 3rem auto; line-height: 1.4; }}
input, button {{ font-size: 1rem; padding: 0.4rem; width: 100%; box-sizing: border-box; }}
button {{ margin-top: 0.8rem; }}
</style></head><body>
<h1>Підключити Claude</h1>
<p>Вставте токен з <code>data/secrets/api.token</code>. Claude збереже його як OAuth access token і надсилатиме як Bearer.</p>
<form method="post" action="/oauth/authorize">
<input type="hidden" name="client_id" value="{_esc(client_id)}">
<input type="hidden" name="redirect_uri" value="{_esc(redirect_uri)}">
<input type="hidden" name="response_type" value="code">
<input type="hidden" name="state" value="{_esc(state)}">
<input type="hidden" name="code_challenge" value="{_esc(code_challenge)}">
<input type="hidden" name="code_challenge_method" value="{_esc(code_challenge_method)}">
<input type="hidden" name="scope" value="{_esc(scope)}">
<label>API token<br><input type="password" name="token" required autocomplete="off"></label>
<button type="submit">Дозволити</button>
</form>
</body></html>"""
    return HTMLResponse(html)


@router.post("/oauth/authorize")
async def authorize_submit(
    request: Request,
    token: str = Form(...),
    client_id: str = Form(...),
    redirect_uri: str = Form(...),
    response_type: str = Form("code"),
    state: str = Form(""),
    code_challenge: str = Form(""),
    code_challenge_method: str = Form("S256"),
    scope: str = Form(""),
) -> RedirectResponse:
    _validate_authorize(request, client_id, redirect_uri, response_type, code_challenge, code_challenge_method)
    hashes = request.app.state.token_hashes
    if not token_valid(token.strip(), hashes):
        raise HTTPException(status_code=401, detail="Invalid API token")
    code = secrets.token_urlsafe(24)
    store: OAuthStore = request.app.state.oauth
    with store.lock:
        store.codes[code] = {
            "token": token.strip(),
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "exp": time.time() + 300,
        }
    params = {"code": code}
    if state:
        params["state"] = state
    sep = "&" if urlparse(redirect_uri).query else "?"
    return RedirectResponse(f"{redirect_uri}{sep}{urlencode(params)}", status_code=302)


@router.post("/oauth/token")
async def token_endpoint(request: Request) -> dict[str, Any]:
    form = await request.form()
    grant = str(form.get("grant_type") or "")
    store: OAuthStore = request.app.state.oauth
    if grant == "authorization_code":
        code = str(form.get("code") or "")
        redirect_uri = str(form.get("redirect_uri") or "")
        verifier = str(form.get("code_verifier") or "")
        client_id = str(form.get("client_id") or "")
        with store.lock:
            record = store.codes.pop(code, None)
        if record is None or record["exp"] < time.time():
            raise HTTPException(status_code=400, detail="invalid_grant")
        if record["redirect_uri"] != redirect_uri or record["client_id"] != client_id:
            raise HTTPException(status_code=400, detail="invalid_grant")
        if pkce_challenge(verifier) != record["code_challenge"]:
            raise HTTPException(status_code=400, detail="invalid_grant")
        access = record["token"]
    elif grant == "refresh_token":
        refresh = str(form.get("refresh_token") or "")
        with store.lock:
            access = store.refresh.get(refresh)
        if not access:
            raise HTTPException(status_code=400, detail="invalid_grant")
    else:
        raise HTTPException(status_code=400, detail="unsupported_grant_type")
    refresh_token = secrets.token_urlsafe(24)
    with store.lock:
        store.refresh[refresh_token] = access
    return {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": 30 * 24 * 3600,
        "refresh_token": refresh_token,
        "scope": "court-search",
    }


def mcp_www_authenticate(origin: str) -> str:
    metadata = f'{origin}/.well-known/oauth-protected-resource'
    return f'Bearer realm="court-search", resource_metadata="{metadata}"'


def _validate_authorize(
    request: Request,
    client_id: str,
    redirect_uri: str,
    response_type: str,
    code_challenge: str,
    code_challenge_method: str,
) -> None:
    if response_type != "code":
        raise HTTPException(status_code=400, detail="response_type must be code")
    if code_challenge_method != "S256" or not code_challenge:
        raise HTTPException(status_code=400, detail="S256 PKCE is required")
    store: OAuthStore = request.app.state.oauth
    with store.lock:
        client = store.clients.get(client_id)
    if client is None:
        raise HTTPException(status_code=400, detail="unknown client_id")
    if redirect_uri not in client["redirect_uris"] and not _loopback_ok(redirect_uri, client["redirect_uris"]):
        raise HTTPException(status_code=400, detail="redirect_uri is not registered")


def _loopback_ok(redirect_uri: str, registered: list[str]) -> bool:
    got = urlparse(redirect_uri)
    if got.hostname not in {"127.0.0.1", "localhost"}:
        return False
    for item in registered:
        parsed = urlparse(item)
        if parsed.hostname in {"127.0.0.1", "localhost"} and parsed.path == got.path:
            return True
    return False


def _esc(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
