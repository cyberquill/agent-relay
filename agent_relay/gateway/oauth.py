"""OAuth 2.1 for a remote MCP server: discovery, client registration, PKCE, code exchange, refresh.

Plain functions over an `httpx.AsyncClient`; nothing here stores or posts anything.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from urllib.parse import urlencode, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field

from agent_relay.log import get_logger

log = get_logger(__name__)

TIMEOUT_S = 20.0
CLIENT_NAME = "Agent Relay"


class OAuthError(Exception):
    """A step of the sign-in failed. The message is safe to show the user."""


class Discovery(BaseModel):
    model_config = ConfigDict(frozen=True)

    resource: str
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str | None = None
    scopes: tuple[str, ...] = ()


class ClientInfo(BaseModel):
    """The OAuth client the relay signs in as: registered on the fly, or one the user supplied."""

    model_config = ConfigDict(frozen=True)

    client_id: str
    client_secret: str | None = None
    redirect_uri: str


class Tokens(BaseModel):
    model_config = ConfigDict(frozen=True)

    access_token: str
    refresh_token: str | None = None
    expires_at: float | None = None
    scope: str = ""

    def expires_within(self, seconds: float, now: float | None = None) -> bool:
        if self.expires_at is None:
            return False
        return self.expires_at - (time.time() if now is None else now) <= seconds


class Pkce(BaseModel):
    model_config = ConfigDict(frozen=True)

    verifier: str = Field(repr=False)
    challenge: str


def new_pkce() -> Pkce:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return Pkce(verifier=verifier, challenge=base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii"))


def new_state() -> str:
    return secrets.token_urlsafe(24)


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


async def _get_json(http: httpx.AsyncClient, url: str) -> dict[str, object] | None:
    try:
        response = await http.get(url, headers={"Accept": "application/json"}, timeout=TIMEOUT_S)
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    try:
        data = response.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _strings(value: object) -> tuple[str, ...]:
    return tuple(v for v in value if isinstance(v, str)) if isinstance(value, list) else ()


async def _resource_metadata(http: httpx.AsyncClient, upstream: str) -> dict[str, object] | None:
    parts = urlsplit(upstream)
    path = parts.path.rstrip("/")
    for suffix in (path, ""):
        found = await _get_json(http, f"{_origin(upstream)}/.well-known/oauth-protected-resource{suffix}")
        if found:
            return found
    return None


async def _server_metadata(http: httpx.AsyncClient, issuer: str) -> dict[str, object] | None:
    parts = urlsplit(issuer)
    path = parts.path.rstrip("/")
    origin = _origin(issuer)
    for url in (
        f"{origin}/.well-known/oauth-authorization-server{path}",
        f"{origin}/.well-known/openid-configuration{path}",
        f"{issuer.rstrip('/')}/.well-known/openid-configuration",
    ):
        found = await _get_json(http, url)
        if found and isinstance(found.get("authorization_endpoint"), str):
            return found
    return None


async def discover(http: httpx.AsyncClient, upstream: str) -> Discovery:
    """What the MCP server at `upstream` says about how to sign in (RFC 9728, then RFC 8414)."""
    resource = await _resource_metadata(http, upstream)
    servers = _strings(resource.get("authorization_servers")) if resource else ()
    issuer = servers[0] if servers else _origin(upstream)
    meta = await _server_metadata(http, issuer)
    if meta is None:
        raise OAuthError(f"{urlsplit(upstream).netloc} does not publish how to sign in")
    authorize, token = meta.get("authorization_endpoint"), meta.get("token_endpoint")
    if not isinstance(authorize, str) or not isinstance(token, str):
        raise OAuthError(f"{urlsplit(upstream).netloc} has no usable authorization endpoints")
    registration = meta.get("registration_endpoint")
    scopes = _strings(resource.get("scopes_supported")) if resource else ()
    declared = resource.get("resource") if resource else None
    return Discovery(
        resource=declared if isinstance(declared, str) else upstream,
        issuer=issuer,
        authorization_endpoint=authorize,
        token_endpoint=token,
        registration_endpoint=registration if isinstance(registration, str) else None,
        scopes=scopes,
    )


async def register(http: httpx.AsyncClient, found: Discovery, redirect_uri: str) -> ClientInfo:
    """Dynamic client registration (RFC 7591), as a public client."""
    if found.registration_endpoint is None:
        raise OAuthError("the server does not allow registering a client: supply a client id")
    body = {
        "client_name": CLIENT_NAME,
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    try:
        response = await http.post(found.registration_endpoint, json=body, timeout=TIMEOUT_S)
    except httpx.HTTPError as exc:
        raise OAuthError(f"registering with the server failed ({exc.__class__.__name__})") from exc
    if response.status_code not in (200, 201):
        raise OAuthError(f"the server refused to register a client (HTTP {response.status_code})")
    data = response.json()
    client_id = data.get("client_id") if isinstance(data, dict) else None
    if not isinstance(client_id, str):
        raise OAuthError("the server's registration answer has no client id")
    secret = data.get("client_secret")
    return ClientInfo(
        client_id=client_id,
        client_secret=secret if isinstance(secret, str) else None,
        redirect_uri=redirect_uri,
    )


def authorize_url(found: Discovery, client: ClientInfo, state: str, pkce: Pkce) -> str:
    query = {
        "response_type": "code",
        "client_id": client.client_id,
        "redirect_uri": client.redirect_uri,
        "state": state,
        "code_challenge": pkce.challenge,
        "code_challenge_method": "S256",
        "resource": found.resource,
    }
    if found.scopes:
        query["scope"] = " ".join(found.scopes)
    return f"{found.authorization_endpoint}?{urlencode(query)}"


def _tokens(data: object, now: float | None, previous_refresh: str | None = None) -> Tokens:
    if not isinstance(data, dict) or not isinstance(data.get("access_token"), str):
        raise OAuthError("the server's token answer has no access token")
    lifetime = data.get("expires_in")
    refresh = data.get("refresh_token")
    scope = data.get("scope")
    return Tokens(
        access_token=data["access_token"],
        refresh_token=refresh if isinstance(refresh, str) else previous_refresh,
        expires_at=(time.time() if now is None else now) + float(lifetime)
        if isinstance(lifetime, int | float)
        else None,
        scope=scope if isinstance(scope, str) else "",
    )


async def _token_request(
    http: httpx.AsyncClient, found: Discovery, client: ClientInfo, form: dict[str, str]
) -> object:
    form = {**form, "client_id": client.client_id}
    if client.client_secret:
        form["client_secret"] = client.client_secret
    try:
        response = await http.post(found.token_endpoint, data=form, timeout=TIMEOUT_S)
    except httpx.HTTPError as exc:
        raise OAuthError(f"the token request failed ({exc.__class__.__name__})") from exc
    if response.status_code != 200:
        log.warning(f"token endpoint answered HTTP {response.status_code} for {form.get('grant_type')}")
        raise OAuthError(f"the server refused the token request (HTTP {response.status_code})")
    try:
        return response.json()
    except ValueError as exc:
        raise OAuthError("the server's token answer is not JSON") from exc


async def exchange(
    http: httpx.AsyncClient,
    found: Discovery,
    client: ClientInfo,
    code: str,
    pkce: Pkce,
    now: float | None = None,
) -> Tokens:
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": client.redirect_uri,
        "code_verifier": pkce.verifier,
        "resource": found.resource,
    }
    return _tokens(await _token_request(http, found, client, form), now)


async def refresh(
    http: httpx.AsyncClient,
    found: Discovery,
    client: ClientInfo,
    tokens: Tokens,
    now: float | None = None,
) -> Tokens:
    if not tokens.refresh_token:
        raise OAuthError("there is no refresh token: sign in again")
    form = {"grant_type": "refresh_token", "refresh_token": tokens.refresh_token, "resource": found.resource}
    return _tokens(await _token_request(http, found, client, form), now, tokens.refresh_token)
