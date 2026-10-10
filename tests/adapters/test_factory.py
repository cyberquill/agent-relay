"""The adapter set follows relay.toml: Cursor CLI on by default, the CDP adapter only when enabled."""

from __future__ import annotations

from agent_relay.adapters.factory import build_adapters
from agent_relay.adapters.vscode.hub import BridgeHub
from agent_relay.core.registry import Registry
from agent_relay.models.config import AdaptersConfig, CdpConfig, RelayConfig
from agent_relay.models.events import Harness


def _config(cdp: bool) -> RelayConfig:
    return RelayConfig(adapters=AdaptersConfig(opencode=False), cdp=CdpConfig(enabled=cdp))


async def test_default_set_has_no_cdp(registry: Registry) -> None:
    built = build_adapters(_config(cdp=False), registry, BridgeHub())
    assert [a.harness for a in built] == [Harness.VSCODE_COPILOT, Harness.VSCODE, Harness.CURSOR_CLI]
    for adapter in built:
        await adapter.aclose()


async def test_cdp_is_added_when_enabled(registry: Registry) -> None:
    built = build_adapters(_config(cdp=True), registry)
    assert [a.harness for a in built] == [Harness.VSCODE_COPILOT, Harness.CURSOR_CLI, Harness.CURSOR_CDP]
    for adapter in built:
        await adapter.aclose()
