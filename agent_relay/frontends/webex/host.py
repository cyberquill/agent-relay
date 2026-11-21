"""The stable Webex object relayd holds, and the supervisor that keeps a front end attached to it.

Webex can fail at start-up (a rejected token) or later (the network, a dropped device). None of that may
stop relayd: its IPC socket keeps answering, so hooks fail open fast and the doctor can say what is wrong.
`WebexHost` is the router sink and the posting handle the daemon passes around; behind it the supervisor
builds a front end, runs its websocket and ticker, and rebuilds with backoff whenever they end.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Literal, Protocol

import httpx
from pydantic import JsonValue

from agent_relay.frontends.webex.api import Message, WebexError
from agent_relay.frontends.webex.ws_client import DeviceError
from agent_relay.log import get_logger
from agent_relay.models.events import Event
from agent_relay.models.sessions import SessionRecord
from agent_relay.scrub import scrub_text

log = get_logger(__name__)

type HostState = Literal["starting", "connected", "rejected", "unreachable"]
type PostedFile = tuple[str, bytes, str]

MIN_BACKOFF_S = 1.0
MAX_BACKOFF_S = 300.0
OBSERVE_EVERY_S = 1.0
REJECTED_STATUSES = frozenset({401, 403})
TOKEN_REJECTED = "Webex token rejected (401)"
EXPECTED_ERRORS = (WebexError, DeviceError, httpx.HTTPError, OSError, TimeoutError)


class HostRenderer(Protocol):
    """What the host forwards to: the attached front end's renderer."""

    async def handle(self, event: Event) -> None: ...

    async def post_top(self, markdown: str, card: dict[str, JsonValue] | None = None) -> Message: ...

    async def post_in_thread(
        self,
        session: SessionRecord,
        markdown: str,
        card: dict[str, JsonValue] | None = None,
        file: PostedFile | None = None,
    ) -> Message: ...

    async def refresh_root(self, session: SessionRecord) -> None: ...

    async def thread_for(self, session: SessionRecord) -> str: ...


class HostFrontend(Protocol):
    @property
    def renderer(self) -> HostRenderer: ...

    async def ticker(self, interval: float = 1.0) -> None: ...


class HostSocket(Protocol):
    """The websocket client, as far as the host and the doctor read it."""

    connected: bool
    connections: int
    last_error: str | None

    async def run(self) -> None: ...


type Built = tuple[HostFrontend, HostSocket]
type Build = Callable[[], Awaitable[Built]]


def classify(exc: BaseException) -> tuple[HostState, str]:
    """The host state a failure means, with the text the doctor shows (scrubbed)."""
    if isinstance(exc, WebexError) and exc.status in REJECTED_STATUSES:
        return "rejected", TOKEN_REJECTED
    if isinstance(exc, DeviceError) and "(401)" in str(exc):
        return "rejected", TOKEN_REJECTED
    return "unreachable", scrub_text(f"{type(exc).__name__}: {exc}")


class WebexHost:
    """Router sink and posting handle that outlives every front end built behind it."""

    def __init__(
        self,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        backoff: tuple[float, float] = (MIN_BACKOFF_S, MAX_BACKOFF_S),
        observe_every_s: float = OBSERVE_EVERY_S,
    ) -> None:
        self._sleep = sleep
        self._backoff_min, self._backoff_max = backoff
        self._observe_every_s = observe_every_s
        self._frontend: HostFrontend | None = None
        self._ws: HostSocket | None = None
        self._state: HostState = "starting"
        self._error: str | None = None
        self._connections = 0
        self._logged: tuple[HostState, str | None] | None = None

    # what the doctor reads

    @property
    def state(self) -> HostState:
        ws = self._ws
        if ws is None:
            return self._state
        if ws.connected:
            return "connected"
        return "unreachable" if ws.last_error else "starting"

    @property
    def last_error(self) -> str | None:
        ws = self._ws
        if ws is None:
            return self._error
        return None if ws.connected else ws.last_error

    @property
    def connections(self) -> int:
        return self._ws.connections if self._ws is not None else self._connections

    @property
    def attached(self) -> bool:
        return self._frontend is not None

    def attach(self, frontend: HostFrontend, ws: HostSocket) -> None:
        self._frontend, self._ws = frontend, ws
        self._error = None

    def detach(self) -> None:
        ws = self._ws
        if ws is not None:
            self._connections = ws.connections
            self._state = "unreachable"
            self._error = ws.last_error or self._error
        self._frontend, self._ws = None, None

    # the router sink and the posting handle: everything is dropped silently while nothing is attached

    @property
    def _renderer(self) -> HostRenderer | None:
        return self._frontend.renderer if self._frontend is not None else None

    async def handle(self, event: Event) -> None:
        renderer = self._renderer
        if renderer is not None:
            await renderer.handle(event)

    async def post_top(self, markdown: str, card: dict[str, JsonValue] | None = None) -> Message | None:
        renderer = self._renderer
        return await renderer.post_top(markdown, card) if renderer is not None else None

    async def post_in_thread(
        self,
        session: SessionRecord,
        markdown: str,
        card: dict[str, JsonValue] | None = None,
        file: PostedFile | None = None,
    ) -> Message | None:
        renderer = self._renderer
        if renderer is None:
            return None
        return await renderer.post_in_thread(session, markdown, card, file)

    async def refresh_root(self, session: SessionRecord) -> None:
        renderer = self._renderer
        if renderer is not None:
            await renderer.refresh_root(session)

    async def thread_for(self, session: SessionRecord) -> str | None:
        renderer = self._renderer
        return await renderer.thread_for(session) if renderer is not None else None

    # the supervisor

    def _observe(self) -> None:
        """Log a change of state or error once, however often it is seen."""
        seen = (self.state, self.last_error)
        if seen == self._logged:
            return
        self._logged = seen
        state, error = seen
        if state == "connected":
            log.info("Webex connected")
        elif state == "rejected":
            log.error(f"{error}; relayd stays up and retries. Fix: pbpaste | relay setup token --stdin")
        elif state == "unreachable":
            log.warning(f"Webex unreachable: {error}; relayd stays up and retries")

    def _failed(self, exc: BaseException) -> None:
        self._state, self._error = classify(exc)
        if not isinstance(exc, EXPECTED_ERRORS):
            log.error(f"Webex front end failed unexpectedly: {type(exc).__name__}", exc_info=exc)
        self._observe()

    async def _watch(self) -> None:
        while True:
            self._observe()
            await asyncio.sleep(self._observe_every_s)

    async def _serve(self, frontend: HostFrontend, ws: HostSocket) -> BaseException | None:
        """Run one attached front end until its websocket or ticker ends; returns why, if it raised."""
        tasks = [
            asyncio.create_task(ws.run(), name="webex-ws"),
            asyncio.create_task(frontend.ticker(), name="renderer-tick"),
            asyncio.create_task(self._watch(), name="webex-watch"),
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        for task in done:
            if not task.cancelled() and task.exception() is not None:
                return task.exception()
        return None

    async def run(self, build: Build) -> None:
        """Build, run and rebuild forever. Only cancellation ends it; no Webex failure does."""
        delay = self._backoff_min
        while True:
            try:
                frontend, ws = await build()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._failed(exc)
            else:
                self.attach(frontend, ws)
                try:
                    failure = await self._serve(frontend, ws)
                finally:
                    connected_before = ws.connections > 0
                    self.detach()
                if failure is not None:
                    self._failed(failure)
                else:
                    self._state, self._error = "unreachable", "the Webex connection ended"
                    self._observe()
                if connected_before:
                    delay = self._backoff_min
            await self._sleep(delay)
            delay = min(delay * 2, self._backoff_max)
