"""Run the PWA inside relayd: only when enabled, only on loopback, only with a Keychain token."""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass

import uvicorn
from fastapi import FastAPI

from agent_relay.core.router import Router
from agent_relay.core.service import RelayService
from agent_relay.frontends.pwa.app import build_app
from agent_relay.frontends.pwa.hub import PwaHub
from agent_relay.frontends.webex.handlers import Handlers
from agent_relay.log import get_logger
from agent_relay.models.config import PwaConfig

log = get_logger(__name__)

LOOPBACK = "127.0.0.1"


class _Server(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        """relayd owns SIGTERM and SIGINT; uvicorn must not take them over."""
        yield


class PwaServer:
    def __init__(self, app: FastAPI, port: int) -> None:
        config = uvicorn.Config(
            app,
            host=LOOPBACK,
            port=port,
            log_config=None,
            access_log=False,
            lifespan="off",
            server_header=False,
            date_header=False,
        )
        self._server = _Server(config)

    async def run(self) -> None:
        log.info(f"PWA listening on http://{LOOPBACK}:{self._server.config.port}")
        await self._server.serve()

    @property
    def started(self) -> bool:
        return self._server.started

    def bound(self) -> list[tuple[str, int]]:
        found: list[tuple[str, int]] = []
        for server in self._server.servers:
            for sock in server.sockets:
                host, port = sock.getsockname()[:2]
                found.append((str(host), int(port)))
        return found

    def stop(self) -> None:
        self._server.should_exit = True


@dataclass
class Pwa:
    hub: PwaHub
    handlers: Handlers
    server: PwaServer


def build_pwa(
    config: PwaConfig,
    service: RelayService,
    router: Router,
    token: str | None,
    doctor: Callable[[], Awaitable[str]] | None = None,
) -> Pwa | None:
    """None when the PWA is off: nothing is created, so nothing binds."""
    if not config.enabled:
        return None
    if not token:
        raise ValueError("pwa.enabled is true but there is no PWA token in the Keychain")
    hub = PwaHub()
    handlers = Handlers(service, hub, doctor=doctor)
    router.subscribe(hub)
    return Pwa(hub, handlers, PwaServer(build_app(hub, service, handlers, token), config.port))
