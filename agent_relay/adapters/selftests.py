"""Self-tests the update watcher re-runs after an app update. None of them starts an app."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from agent_relay.adapters.ahp.endpoints import AhpUnavailableError, Endpoint, ProtocolMismatchError, discover
from agent_relay.core.registry import Registry
from agent_relay.core.updates import Selftest, SelftestResult
from agent_relay.models.config import RelayConfig
from agent_relay.models.events import Harness

type Discoverer = Callable[[], Awaitable[Endpoint]]


def ahp_selftest(discoverer: Discoverer = discover) -> Selftest:
    async def check() -> SelftestResult:
        try:
            endpoint = await discoverer()
        except ProtocolMismatchError as exc:
            return "fail", str(exc)
        except AhpUnavailableError as exc:
            return "unknown", str(exc)
        return "pass", f"AHP {endpoint.protocol_version}"

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
