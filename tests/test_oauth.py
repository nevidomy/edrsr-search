from court_search.api.oauth import pkce_challenge
from court_search.api.auth import hash_token, token_valid


def test_pkce_s256_is_stable() -> None:
    assert pkce_challenge("verifier-one") == pkce_challenge("verifier-one")
    assert pkce_challenge("verifier-one") != pkce_challenge("verifier-two")


def test_token_valid_matches_hash() -> None:
    token = "secret-token"
    assert token_valid(token, {hash_token(token)})
    assert not token_valid("other", {hash_token(token)})
