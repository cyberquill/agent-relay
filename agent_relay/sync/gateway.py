"""Pointing apps at the gateway: a gatewayed server is rendered as a loopback URL plus the gateway key."""

from __future__ import annotations

import re

from agent_relay.gateway.store import KEY_HEADER, KEY_REF
from agent_relay.sync.model import Catalog, CatalogEntry, ServerDef

GATEWAY_URL = re.compile(r"^https?://(?:127\.0\.0\.1|localhost|\[::1\]):\d+/mcp/(?P<name>[^/?#]+)")


def gateway_url(port: int, name: str) -> str:
    return f"http://127.0.0.1:{port}/mcp/{name}"


def point_at_gateway(source: Catalog, port: int) -> Catalog:
    """Every gatewayed entry with an upstream is rendered through the gateway in every app.

    The per-app variants are dropped: what differed between apps was how each one signed in itself.
    """
    servers: dict[str, CatalogEntry] = {}
    for name, entry in source.servers.items():
        if entry.upstream() is None:
            servers[name] = entry
            continue
        routed = ServerDef(
            transport="http",
            url=gateway_url(port, name),
            headers={KEY_HEADER: KEY_REF},
            timeout_ms=entry.server.timeout_ms,
        )
        servers[name] = entry.model_copy(update={"server": routed, "variants": {}})
    return source.model_copy(update={"servers": servers})


def _points_at_gateway(entry: CatalogEntry, name: str) -> bool:
    urls = [entry.server.url, *(v.url for v in entry.variants.values())]
    return any((m := GATEWAY_URL.match(u or "")) and m["name"] == name for u in urls)


def keep_gateway_entries(imported: Catalog, previous: Catalog) -> Catalog:
    """An import reads the native files back. A server they show only as its gateway address keeps the
    entry it had (with its real upstream); a gatewayed server defined some other way keeps its flag."""
    servers = dict(imported.servers)
    for name, before in previous.servers.items():
        if not before.gateway or name not in servers:
            continue
        now = servers[name]
        if _points_at_gateway(now, name):
            servers[name] = before
        else:
            servers[name] = now.model_copy(update={"gateway": True, "gateway_url": before.gateway_url})
    return imported.model_copy(update={"servers": servers})
