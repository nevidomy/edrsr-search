from court_search.api.oauth import pkce_challenge
from court_search.api.auth import hash_token, token_valid


def test_pkce_s256_is_stable() -> None:
    assert pkce_challenge("verifier-one") == pkce_challenge("verifier-one")
    assert pkce_challenge("verifier-one") != pkce_challenge("verifier-two")


def test_token_valid_matches_hash() -> None:
    token = "secret-token"
    assert token_valid(token, {hash_token(token)})
    assert not token_valid("other", {hash_token(token)})


def test_expired_codes_are_pruned() -> None:
    from court_search.api.oauth import OAuthStore, prune_codes

    store = OAuthStore()
    store.codes = {"old": {"exp": 100.0}, "fresh": {"exp": 300.0}}
    prune_codes(store, now=200.0)
    assert list(store.codes) == ["fresh"]


def test_registration_is_bounded() -> None:
    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    from court_search.api import oauth

    app = FastAPI()
    app.include_router(oauth.router)
    app.state.oauth = oauth.OAuthStore()
    client = TestClient(app)
    assert client.post("/oauth/register", json={"redirect_uris": ["https://claude.ai/cb"]}).status_code == 200
    assert client.post("/oauth/register", json={"redirect_uris": ["x" * 3000]}).status_code == 400
    app.state.oauth.clients = {str(i): {} for i in range(oauth.MAX_CLIENTS)}
    assert client.post("/oauth/register", json={"redirect_uris": ["https://claude.ai/cb"]}).status_code == 429
