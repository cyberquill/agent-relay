"""The PWA's HTTP surface: one static page, a bearer-token API, and an SSE stream.

Every `/api` route needs `Authorization: Bearer <pwa-token>`. The page itself carries no data, so it is
served without a token; it asks for one and keeps it in `sessionStorage`.
"""

from __future__ import annotations

import asyncio
import hmac
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field, JsonValue
from starlette.responses import Response

from agent_relay.core.service import RelayError, RelayService
from agent_relay.frontends import actions
from agent_relay.frontends.pwa.hub import KEEPALIVE_S, PwaHub, sse
from agent_relay.frontends.webex import cards
from agent_relay.frontends.webex.handlers import Context, Handlers
from agent_relay.log import get_logger
from agent_relay.scrub import scrub_text

log = get_logger(__name__)

STATIC = Path(__file__).parent / "static"
PAGES: dict[str, tuple[str, str]] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
}
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}

type Disconnected = Callable[[], Awaitable[bool]]


class SendBody(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)
    session: str | None = None


class CardBody(BaseModel):
    inputs: dict[str, JsonValue]


async def stream(
    hub: PwaHub, disconnected: Disconnected, keepalive_s: float = KEEPALIVE_S
) -> AsyncIterator[bytes]:
    async with hub.subscribe() as queue:
        yield b": connected\n\n"
        while not await disconnected():
            try:
                payload = await asyncio.wait_for(queue.get(), keepalive_s)
            except TimeoutError:
                yield b": keepalive\n\n"
                continue
            yield sse(payload)


def build_app(hub: PwaHub, service: RelayService, handlers: Handlers, token: str) -> FastAPI:
    if not token:
        raise ValueError("the PWA needs a non-empty bearer token")
    expected = token.encode()
    app = FastAPI(title="agent-relay", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def headers(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    def authorized(request: Request) -> None:
        scheme, _, given = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(given.strip().encode(), expected):
            raise HTTPException(401, "missing or wrong bearer token", {"WWW-Authenticate": "Bearer"})

    for route, (name, media) in PAGES.items():
        app.add_api_route(route, _page(name, media), methods=["GET"], include_in_schema=False)

    api = APIRouter(prefix="/api", dependencies=[Depends(authorized)])

    @api.get("/sessions")
    async def sessions() -> list[dict[str, JsonValue]]:
        return [
            {
                "session": s.session_key,
                "app": s.harness.value,
                "workspace": s.workspace,
                "status": s.status.value,
                "root": scrub_text(cards.root_markdown(s)),
            }
            for s in await service.sessions()
        ]

    @api.post("/send", status_code=202)
    async def send(body: SendBody) -> dict[str, JsonValue]:
        session = None
        if body.session is not None:
            try:
                session = await service.session(body.session)
            except RelayError as exc:
                raise HTTPException(404, str(exc)) from exc
        await handlers.text(Context(session), body.text)
        return {"ok": True}

    @api.post("/card")
    async def card(body: CardBody) -> dict[str, JsonValue]:
        return {"outcome": await actions.submit(service, handlers, body.inputs)}

    @api.get("/events")
    async def events(request: Request) -> StreamingResponse:
        return StreamingResponse(
            stream(hub, request.is_disconnected),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"},
        )

    app.include_router(api)
    return app


def _page(name: str, media: str) -> Callable[[], Awaitable[FileResponse]]:
    async def page() -> FileResponse:
        return FileResponse(STATIC / name, media_type=media)

    return page
