"""AC-24 (live, RELAY_LIVE=1): `relay selftest cursor-cdp` against the real Cursor.

Read-only: it counts selector matches and creates no chat. Cursor must already run with
`--remote-debugging-port=9223` (or be closed, so the launcher starts it with the port) and have its chat
pane open. The result is recorded in a temporary registry, never the real one.
"""

from __future__ import annotations

from agent_relay.adapters.cursor_cdp.connect import fetch_version
from agent_relay.adapters.cursor_cdp.selectors import load_selectors
from agent_relay.adapters.cursor_cdp.selftest import run_live
from agent_relay.core.registry import Registry
from agent_relay.models.config import CdpConfig
from tests.conftest import live

pytestmark = live

PORT = CdpConfig().port


async def test_live_selftest_passes_against_cursor(registry: Registry) -> None:
    report = await run_live(registry, PORT, load_selectors())
    assert report.ok, report.render()
    assert await fetch_version(PORT) == report.version
