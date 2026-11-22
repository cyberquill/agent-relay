"""The OAuth steps against a fake MCP server: discovery, registration, PKCE, exchange, refresh."""

from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qs, urlsplit

import pytest

from agent_relay.gateway import oauth
from agent_relay.gateway.oauth import ClientInfo, OAuthError, Tokens
from tests.fakes.oauth_server import HOST, UPSTREAM, FakeOAuthServer

REDIRECT = "http://127.0.0.1:8791/oauth/callback"


async def test_discovery_follows_the_protected_resource_to_its_authorization_server() -> None:
    server = FakeOAuthServer()
    found = await oauth.discover(server.client(), UPSTREAM)
    assert found.issuer == HOST
    assert found.authorization_endpoint == f"{HOST}/authorize"
    assert found.registration_endpoint == f"{HOST}/register"
    assert found.scopes == ("mcp:read",)


async def test_discovery_without_resource_metadata_falls_back_to_the_origin() -> None:
    server = FakeOAuthServer()
    found = await oauth.discover(server.client(), f"{HOST}/other")
    assert found.issuer == HOST
    assert found.resource == f"{HOST}/other"


async def test_a_server_that_publishes_nothing_is_reported() -> None:
    server = FakeOAuthServer()
    with pytest.raises(OAuthError, match="does not publish"):
        await oauth.discover(server.client(), "https://nothing.example.test/mcp")


async def test_registration_is_a_public_client_with_the_gateway_redirect() -> None:
    server = FakeOAuthServer()
    found = await oauth.discover(server.client(), UPSTREAM)
    client = await oauth.register(server.client(), found, REDIRECT)
    assert client == ClientInfo(client_id="client-1", redirect_uri=REDIRECT)
    body = server.registered[0]
    assert body["redirect_uris"] == [REDIRECT]
    assert body["token_endpoint_auth_method"] == "none"


async def test_registration_without_an_endpoint_asks_for_a_client_id() -> None:
    server = FakeOAuthServer(registration=False)
    found = await oauth.discover(server.client(), UPSTREAM)
    with pytest.raises(OAuthError, match="supply a client id"):
        await oauth.register(server.client(), found, REDIRECT)


def test_pkce_challenge_is_the_sha256_of_the_verifier() -> None:
    pkce = oauth.new_pkce()
    digest = hashlib.sha256(pkce.verifier.encode()).digest()
    assert pkce.challenge == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    assert oauth.new_pkce().verifier != pkce.verifier


async def test_the_authorize_link_carries_pkce_state_and_resource() -> None:
    server = FakeOAuthServer()
    found = await oauth.discover(server.client(), UPSTREAM)
    client = ClientInfo(client_id="c", redirect_uri=REDIRECT)
    pkce = oauth.new_pkce()
    link = oauth.authorize_url(found, client, "st", pkce)
    query = {k: v[0] for k, v in parse_qs(urlsplit(link).query).items()}
    assert link.startswith(f"{HOST}/authorize?")
    assert query["response_type"] == "code"
    assert query["code_challenge"] == pkce.challenge
    assert query["code_challenge_method"] == "S256"
    assert query["state"] == "st"
    assert query["resource"] == UPSTREAM
    assert query["scope"] == "mcp:read"


async def test_a_code_is_traded_for_tokens_and_a_wrong_verifier_is_refused() -> None:
    server = FakeOAuthServer()
    http = server.client()
    found = await oauth.discover(http, UPSTREAM)
    client = await oauth.register(http, found, REDIRECT)
    pkce = oauth.new_pkce()
    tokens = await oauth.exchange(http, found, client, server.grant_code(pkce.challenge), pkce)
    assert tokens.access_token == "access-1"
    assert tokens.refresh_token == "refresh-1"
    assert tokens.expires_at is not None
    with pytest.raises(OAuthError, match="refused the token request"):
        await oauth.exchange(http, found, client, server.grant_code(pkce.challenge), oauth.new_pkce())


async def test_refresh_keeps_the_old_refresh_token_when_none_is_returned() -> None:
    server = FakeOAuthServer()
    http = server.client()
    found = await oauth.discover(http, UPSTREAM)
    client = ClientInfo(client_id="c", redirect_uri=REDIRECT)
    old = Tokens(access_token="old", refresh_token=server.refresh_token)
    fresh = await oauth.refresh(http, found, client, old)
    assert fresh.access_token == "access-1"
    assert fresh.refresh_token == "refresh-1"


async def test_refresh_needs_a_refresh_token_and_reports_a_refusal() -> None:
    server = FakeOAuthServer()
    http = server.client()
    found = await oauth.discover(http, UPSTREAM)
    client = ClientInfo(client_id="c", redirect_uri=REDIRECT)
    with pytest.raises(OAuthError, match="no refresh token"):
        await oauth.refresh(http, found, client, Tokens(access_token="a"))
    with pytest.raises(OAuthError, match="refused"):
        await oauth.refresh(http, found, client, Tokens(access_token="a", refresh_token="wrong"))


def test_tokens_know_when_they_are_about_to_expire() -> None:
    tokens = Tokens(access_token="a", expires_at=1000.0)
    assert tokens.expires_within(120, now=900.0)
    assert not tokens.expires_within(60, now=900.0)
    assert not Tokens(access_token="a").expires_within(10_000, now=0.0)
