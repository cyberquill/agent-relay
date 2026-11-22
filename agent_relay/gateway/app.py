"""The gateway's HTTP surface on 127.0.0.1.

- `/mcp/<name>[/...]`: a reverse proxy to the server named `<name>`. An app sends the gateway key; the
  gateway swaps it for the server's access token, so the app never holds one.
- `/oauth/callback`: where a browser on the Mac lands after a sign-in.
"""

from __future__ import annotations

import hmac
from collections.abc import AsyncIterator, Callable
from html import escape

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from starlette.background import BackgroundTask
from starlette.responses import Response

from agent_relay.gateway.oauth import OAuthError
from agent_relay.gateway.signin import CALLBACK_PATH, SignIn
from agent_relay.gateway.store import KEY_HEADER
from agent_relay.log import get_logger
from agent_relay.scrub import scrub_text

log = get_logger(__name__)

NOT_FORWARDED = frozenset(
    {
        "host",
        "authorization",
        "cookie",
        "content-length",
        "connection",
        "keep-alive",
        "transfer-encoding",
        "upgrade",
        "accept-encoding",
        KEY_HEADER.lower(),
    }
)
NOT_RETURNED = frozenset(
    {
        "www-authenticate",
        "content-length",
        "content-encoding",
        "connection",
        "keep-alive",
        "transfer-encoding",
        "set-cookie",
    }
)
HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}
UPSTREAM_TIMEOUT = httpx.Timeout(30.0, read=None)


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    html = (
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{escape(title)}</title><body style='font:16px system-ui;margin:2rem'>"
        f"<h2>{escape(title)}</h2><p>{escape(body)}</p>"
    )
    return HTMLResponse(html, status, headers=HEADERS)


def _signin_needed(name: str) -> JSONResponse:
    message = f"MCP server {name} needs a sign-in. A link was sent to the phone; open it, then retry."
    return JSONResponse({"error": "signin_required", "message": message}, 401, headers=HEADERS)


def _forward_headers(request: Request, token: str) -> dict[str, str]:
    sent = {k: v for k, v in request.headers.items() if k.lower() not in NOT_FORWARDED}
    sent["authorization"] = f"Bearer {token}"
    sent["accept-encoding"] = "identity"
    return sent


def _upstream_url(base: str, path: str, query: str) -> str:
    url = base.rstrip("/") + (f"/{path}" if path else "")
    return f"{url}?{query}" if query else url


def build_app(
    signin: SignIn, http: httpx.AsyncClient, key: Callable[[], str | None], hold_s: float
) -> FastAPI:
    app = FastAPI(title="agent-relay-gateway", docs_url=None, redoc_url=None, openapi_url=None)

    def allowed(request: Request) -> bool:
        expected = key()
        given = request.headers.get(KEY_HEADER, "")
        return bool(expected) and hmac.compare_digest(given.encode(), (expected or "").encode())

    @app.get(CALLBACK_PATH, include_in_schema=False)
    async def callback(request: Request) -> Response:
        query = request.query_params
        state, code, error = query.get("state", ""), query.get("code", ""), query.get("error", "")
        if not state:
            return _page("Nothing to finish", "This address needs the state a sign-in link carries.", 400)
        if error or not code:
            signin.cancel(state, error or "no code")
            return _page(
                "Sign-in cancelled", "The server did not allow access. Ask for a new link on the phone.", 400
            )
        try:
            name = await signin.complete(state, code)
        except OAuthError as exc:
            return _page("Sign-in failed", str(exc), 400)
        return _page("Signed in", f"{name} is signed in. You can close this tab.")

    @app.api_route(
        "/mcp/{name}",
        methods=["GET", "POST", "DELETE"],
        include_in_schema=False,
    )
    @app.api_route(
        "/mcp/{name}/{path:path}",
        methods=["GET", "POST", "DELETE"],
        include_in_schema=False,
    )
    async def proxy(request: Request, name: str, path: str = "") -> Response:
        if not allowed(request):
            return JSONResponse({"error": "forbidden"}, 403, headers=HEADERS)
        base = signin.upstream(name)
        if base is None:
            return JSONResponse(
                {"error": "unknown_server", "message": f"no MCP server {name}"}, 404, headers=HEADERS
            )
        body = await request.body()
        url = _upstream_url(base, path, request.url.query)
        for attempt in (1, 2):
            try:
                tokens = await signin.ensure(name, hold_s if attempt == 1 else 0)
            except OAuthError as exc:
                log.warning(f"sign-in for MCP server {name} could not start: {exc}")
                return JSONResponse({"error": "signin_failed", "message": str(exc)}, 502, headers=HEADERS)
            if tokens is None:
                return _signin_needed(name)
            upstream = await _send(http, request, url, body, tokens.access_token)
            if upstream is None:
                return JSONResponse({"error": "upstream_unreachable"}, 502, headers=HEADERS)
            if upstream.status_code != 401:
                return _relay(upstream)
            await upstream.aclose()
            await signin.invalidate(name)
        signin.sign_out(name)
        try:
            await signin.begin(name, "refused its sign-in")
        except OAuthError as exc:
            log.warning(f"sign-in for MCP server {name} could not start: {exc}")
        return _signin_needed(name)

    return app


async def _send(
    http: httpx.AsyncClient, request: Request, url: str, body: bytes, token: str
) -> httpx.Response | None:
    built = http.build_request(request.method, url, headers=_forward_headers(request, token), content=body)
    try:
        return await http.send(built, stream=True)
    except httpx.HTTPError as exc:
        log.warning(f"an MCP server could not be reached: {scrub_text(str(exc))}")
        return None


def _relay(upstream: httpx.Response) -> StreamingResponse:
    async def chunks() -> AsyncIterator[bytes]:
        async for chunk in upstream.aiter_bytes():
            yield chunk

    headers = {k: v for k, v in upstream.headers.items() if k.lower() not in NOT_RETURNED}
    return StreamingResponse(
        chunks(),
        status_code=upstream.status_code,
        headers=headers,
        background=BackgroundTask(upstream.aclose),
    )
