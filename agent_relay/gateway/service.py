"""Building the gateway from the config and the MCP catalog."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx

from agent_relay import paths
from agent_relay.frontends.pwa.server import PwaServer
from agent_relay.gateway.app import build_app
from agent_relay.gateway.signin import Notify, SignIn
from agent_relay.gateway.store import CredentialStore, gateway_key
from agent_relay.models.config import GatewayConfig
from agent_relay.sync import catalog


class CatalogServers:
    """The gatewayed servers of `~/.agents/mcp.yaml`, re-read when the file changes."""

    def __init__(self, file: Path | None = None) -> None:
        self._file = file
        self._stamp: tuple[float, int] | None = None
        self._servers: dict[str, str] = {}

    def __call__(self) -> Mapping[str, str]:
        file = self._file or paths.mcp_yaml()
        try:
            info = file.stat()
        except OSError:
            self._stamp, self._servers = None, {}
            return self._servers
        stamp = (info.st_mtime, info.st_size)
        if stamp != self._stamp:
            loaded = catalog.load(file)
            self._servers = {n: u for n, e in loaded.servers.items() if (u := e.upstream())}
            self._stamp = stamp
        return self._servers


@dataclass
class Gateway:
    signin: SignIn
    server: PwaServer
    http: httpx.AsyncClient

    async def close(self) -> None:
        await self.http.aclose()


def build_signin(
    config: GatewayConfig,
    notify: Notify | None = None,
    servers: Callable[[], Mapping[str, str]] | None = None,
    http: httpx.AsyncClient | None = None,
    store: CredentialStore | None = None,
) -> SignIn:
    return SignIn(
        config, store or CredentialStore(), http or httpx.AsyncClient(), servers or CatalogServers(), notify
    )


def build_gateway(
    config: GatewayConfig, signin: SignIn, upstream: httpx.AsyncClient | None = None
) -> Gateway | None:
    """None when the gateway is off: nothing is created, so nothing binds."""
    if not config.enabled:
        return None
    key = gateway_key(create=True)
    if key is None:
        raise ValueError("gateway.enabled is true but the Keychain cannot hold the gateway key")
    client = upstream or httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None))
    app = build_app(signin, client, lambda: key, float(config.signin_hold_s))
    return Gateway(signin, PwaServer(app, config.port, "MCP gateway"), client)
