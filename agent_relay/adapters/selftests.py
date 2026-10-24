"""Self-tests the update watcher re-runs after an app update. None of them starts an app."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from agent_relay import __version__
from agent_relay.adapters.ahp import models as m
from agent_relay.adapters.ahp import uris
from agent_relay.adapters.ahp.endpoints import (
    SUPPORTED_VERSIONS,
    AhpUnavailableError,
    Endpoint,
    ProtocolMismatchError,
    discover,
)
from agent_relay.adapters.ahp.rpc import AhpRpcError, Connection, initialize
from agent_relay.core.registry import Registry
from agent_relay.core.updates import Selftest, SelftestResult
from agent_relay.models.config import RelayConfig
from agent_relay.models.events import Harness

type Discoverer = Callable[[], Awaitable[Endpoint]]
type Opener = Callable[[Endpoint], Awaitable[Connection]]


async def negotiate(endpoint: Endpoint, opener: Opener = Connection.open) -> str:
    """Open a throwaway connection, run `initialize` with no subscriptions and return the agreed version."""
    conn = await opener(endpoint)
    try:
        params = m.InitializeParams(
            channel=uris.ROOT,
            protocol_versions=list(SUPPORTED_VERSIONS),
            client_id=f"agent-relay-selftest-{uuid.uuid4()}",
            client_info=m.Implementation(name="agent-relay-selftest", version=__version__),
            initial_subscriptions=[],
        )
        return (await initialize(conn, endpoint, params)).protocol_version
    finally:
        await conn.close()


def ahp_selftest(discoverer: Discoverer = discover, opener: Opener = Connection.open) -> Selftest:
    async def check() -> SelftestResult:
        try:
            endpoint = await discoverer()
            agreed = await negotiate(endpoint, opener)
        except ProtocolMismatchError as exc:
            return "fail", str(exc)
        except AhpUnavailableError as exc:
            return "unknown", str(exc)
        except AhpRpcError as exc:
            return "fail", str(exc)
        if agreed == endpoint.protocol_version:
            return "pass", f"AHP {agreed}"
        return "pass", f"AHP {agreed}; VS Code also speaks {endpoint.protocol_version}"

    return check


def cursor_cdp_selftest(registry: Registry, port: int) -> Selftest:
    async def check() -> SelftestResult:
        from agent_relay.adapters.cursor_cdp.selectors import load_selectors
        from agent_relay.adapters.cursor_cdp.selftest import check_attached

        report = await check_attached(registry, port, load_selectors())
        if report is None:
            return "unknown", "Cursor is not running with its debug port"
        if not report.ok:
            return "fail", f"selectors not found: {', '.join(report.failures())}"
        return "pass", f"Cursor {report.version}"

    return check


def build_selftests(config: RelayConfig, registry: Registry) -> dict[Harness, Selftest]:
    tests: dict[Harness, Selftest] = {}
    if config.adapters.ahp:
        tests[Harness.VSCODE_COPILOT] = ahp_selftest()
    if config.cdp.enabled:
        tests[Harness.CURSOR_CDP] = cursor_cdp_selftest(registry, config.cdp.port)
    return tests
