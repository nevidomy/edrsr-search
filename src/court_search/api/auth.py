from __future__ import annotations

import hashlib
import hmac
import secrets
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from court_search.config import ApiConfig

_bearer = HTTPBearer(auto_error=False)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def init_secrets(api: ApiConfig) -> str:
    api.token_file.parent.mkdir(parents=True, exist_ok=True)
    if api.token_file.exists():
        token = api.token_file.read_text(encoding="utf-8").strip()
    else:
        token = secrets.token_urlsafe(32)
        api.token_file.write_text(token + "\n", encoding="utf-8")
        api.token_file.chmod(0o600)
    digest = hash_token(token)
    existing = set()
    if api.token_hashes_file.exists():
        existing = {line.strip() for line in api.token_hashes_file.read_text(encoding="utf-8").splitlines() if line.strip()}
    existing.add(digest)
    api.token_hashes_file.write_text("\n".join(sorted(existing)) + "\n", encoding="utf-8")
    api.token_hashes_file.chmod(0o600)
    return token


def load_token_hashes(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}


def token_valid(token: str, hashes: set[str]) -> bool:
    digest = hash_token(token)
    return any(hmac.compare_digest(digest, known) for known in hashes)


def extract_presented_token(request: Request, credentials: HTTPAuthorizationCredentials | None) -> str | None:
    if credentials is not None and credentials.credentials:
        return credentials.credentials
    api_key = request.headers.get("x-api-key") or request.headers.get("x-auth-token")
    if api_key:
        return api_key.strip()
    return None


async def require_token(request: Request) -> None:
    hashes: set[str] = request.app.state.token_hashes
    if not hashes:
        raise HTTPException(status_code=503, detail="API token hashes are not configured")
    credentials: HTTPAuthorizationCredentials | None = await _bearer(request)
    presented = extract_presented_token(request, credentials)
    if presented is None or not token_valid(presented, hashes):
        raise HTTPException(status_code=401, detail="Invalid or missing bearer token")
