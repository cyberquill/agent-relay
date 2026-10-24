"""Self-tests re-run after app updates: outcomes, and that none of them starts an app."""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path

from agent_relay.adapters.ahp.endpoints import SUPPORTED_VERSIONS, AhpUnavailableError, Endpoint
from agent_relay.adapters.ahp.models import PROTOCOL_VERSION
from agent_relay.adapters.selftests import ahp_selftest, build_selftests, cursor_cdp_selftest
from agent_relay.core.registry import Registry
from agent_relay.models.config import AdaptersConfig, CdpConfig, RelayConfig
from agent_relay.models.events import Harness
from tests.fakes.ahp import FakeAgentHost


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


async def test_ahp_selftest_outcomes(relay_home: Path) -> None:
    host = await FakeAgentHost(str(relay_home / "st.sock"), protocol_version="1.1.0").start()

    async def found() -> Endpoint:
        return host.endpoint

    async def closed() -> Endpoint:
        raise AhpUnavailableError("VS Code is not running an Agent Host (open VS Code)")

    try:
        assert await ahp_selftest(found)() == ("pass", f"AHP {PROTOCOL_VERSION}; VS Code also speaks 1.1.0")
        (offered,) = [p["protocolVersions"] for method, p in host.requests if method == "initialize"]
        assert offered == list(SUPPORTED_VERSIONS)
        host.protocol_version = PROTOCOL_VERSION
        assert await ahp_selftest(found)() == ("pass", f"AHP {PROTOCOL_VERSION}")
        host.accepts = ("2.0.0",)
        outcome, detail = await ahp_selftest(found)()
        assert outcome == "fail"
        assert f"refused AHP {PROTOCOL_VERSION}" in detail
        assert (await ahp_selftest(closed)())[0] == "unknown"
        for _ in range(200):
            if not host.clients:
                break
            await asyncio.sleep(0.005)
        assert not host.clients
    finally:
        await host.stop()
    assert (await ahp_selftest(found)())[0] == "unknown"


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
