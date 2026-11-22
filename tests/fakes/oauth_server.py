"""A remote MCP server with OAuth, served through an httpx MockTransport.

It publishes discovery documents, registers clients, trades a code for tokens (checking PKCE), refreshes
them, and answers `/mcp` only for the access token it issued last.
"""

from __future__ import annotations

import base64
import hashlib
import json
from urllib.parse import parse_qs

import httpx

HOST = "https://mcp.example.test"
UPSTREAM = f"{HOST}/v1/mcp"


class FakeOAuthServer:
    def __init__(self, registration: bool = True, expires_in: int = 3600) -> None:
        self.registration = registration
        self.expires_in = expires_in
        self.codes: dict[str, str] = {}
        self.access = "access-0"
        self.refresh_token = "refresh-0"
        self.issued = 0
        self.refused_refresh = False
        self.requests: list[httpx.Request] = []
        self.mcp_bodies: list[bytes] = []
        self.registered: list[dict[str, object]] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.transport())

    def grant_code(self, challenge: str) -> str:
        code = f"code-{len(self.codes) + 1}"
        self.codes[code] = challenge
        return code

    def revoke(self) -> None:
        self.access = "revoked"

    def seen(self, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == path]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.url.host != "mcp.example.test":
            return httpx.Response(404)
        if path == "/.well-known/oauth-protected-resource/v1/mcp":
            return httpx.Response(
                200,
                json={
                    "resource": UPSTREAM,
                    "authorization_servers": [HOST],
                    "scopes_supported": ["mcp:read"],
                },
            )
        if path == "/.well-known/oauth-authorization-server":
            meta = {
                "issuer": HOST,
                "authorization_endpoint": f"{HOST}/authorize",
                "token_endpoint": f"{HOST}/token",
            }
            if self.registration:
                meta["registration_endpoint"] = f"{HOST}/register"
            return httpx.Response(200, json=meta)
        if path == "/register":
            body = json.loads(request.content)
            self.registered.append(body)
            return httpx.Response(201, json={"client_id": "client-1"})
        if path == "/token":
            return self._token(parse_qs(request.content.decode()))
        if path.startswith("/v1/mcp"):
            return self._mcp(request)
        return httpx.Response(404)

    def _issue(self) -> httpx.Response:
        self.issued += 1
        self.access = f"access-{self.issued}"
        self.refresh_token = f"refresh-{self.issued}"
        return httpx.Response(
            200,
            json={
                "access_token": self.access,
                "refresh_token": self.refresh_token,
                "expires_in": self.expires_in,
                "token_type": "Bearer",
            },
        )

    def _token(self, form: dict[str, list[str]]) -> httpx.Response:
        grant = form.get("grant_type", [""])[0]
        if grant == "authorization_code":
            code = form.get("code", [""])[0]
            challenge = self.codes.pop(code, None)
            verifier = form.get("code_verifier", [""])[0]
            digest = hashlib.sha256(verifier.encode()).digest()
            if challenge is None or base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != challenge:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return self._issue()
        if grant == "refresh_token":
            if self.refused_refresh or form.get("refresh_token", [""])[0] != self.refresh_token:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return self._issue()
        return httpx.Response(400, json={"error": "unsupported_grant_type"})

    def _mcp(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != f"Bearer {self.access}":
            return httpx.Response(401, headers={"www-authenticate": 'Bearer resource_metadata="x"'})
        self.mcp_bodies.append(request.content)
        if request.headers.get("accept", "").startswith("text/event-stream"):
            body = b'event: message\ndata: {"ok":true}\n\n'
            return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": 1, "result": {}}, headers={"mcp-session-id": "sess-1"}
        )
