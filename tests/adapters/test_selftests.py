"""Self-tests re-run after app updates: outcomes, and that none of them starts an app."""

from __future__ import annotations

import socket

from agent_relay.adapters.ahp.endpoints import AhpUnavailableError, Endpoint, ProtocolMismatchError
from agent_relay.adapters.selftests import ahp_selftest, build_selftests, cursor_cdp_selftest
from agent_relay.core.registry import Registry
from agent_relay.models.config import AdaptersConfig, CdpConfig, RelayConfig
from agent_relay.models.events import Harness


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


async def test_ahp_selftest_outcomes() -> None:
    async def found() -> Endpoint:
        return Endpoint("/tmp/ahp.sock", "0.9.0", token="redacted-in-repr")

    async def mismatch() -> Endpoint:
        raise ProtocolMismatchError("VS Code speaks AHP 1.0.0; the relay supports 0.9.0")

    async def closed() -> Endpoint:
        raise AhpUnavailableError("VS Code is not running an Agent Host (open VS Code)")

    assert await ahp_selftest(found)() == ("pass", "AHP 0.9.0")
    assert await ahp_selftest(mismatch)() == ("fail", "VS Code speaks AHP 1.0.0; the relay supports 0.9.0")
    assert (await ahp_selftest(closed)())[0] == "unknown"


async def test_cursor_cdp_selftest_is_unknown_without_a_debuggable_cursor(registry: Registry) -> None:
    outcome, detail = await cursor_cdp_selftest(registry, free_port())()
    assert outcome == "unknown"
    assert "not running with its debug port" in detail


def test_build_selftests_follows_the_config(registry: Registry) -> None:
    default = build_selftests(RelayConfig(), registry)
    assert set(default) == {Harness.VSCODE_COPILOT}
    both = build_selftests(RelayConfig(cdp=CdpConfig(enabled=True)), registry)
    assert set(both) == {Harness.VSCODE_COPILOT, Harness.CURSOR_CDP}
    none = build_selftests(RelayConfig(adapters=AdaptersConfig(ahp=False)), registry)
    assert none == {}
