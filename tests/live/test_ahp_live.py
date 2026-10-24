"""AC-16 (live, RELAY_LIVE=1): list sessions on the real VS Code Agent Host.

Read-only: it creates nothing, so it does not use any of the AHP live attempts (all three went to S1).
"""

from __future__ import annotations

from urllib.parse import urlparse

from agent_relay.adapters.ahp import models as m
from agent_relay.adapters.ahp.adapter import AhpAdapter
from agent_relay.adapters.ahp.endpoints import SUPPORTED_VERSIONS, discover
from agent_relay.adapters.ahp.rpc import Connection, initialize
from agent_relay.core.registry import Registry
from tests.conftest import live

pytestmark = live


async def test_lists_sessions_on_the_editor_host(registry: Registry) -> None:
    endpoint = await discover()
    conn = await Connection.open(endpoint)
    try:
        init = await initialize(
            conn,
            endpoint,
            m.InitializeParams(
                channel="ahp-root://",
                protocol_versions=list(SUPPORTED_VERSIONS),
                client_id="agent-relay-live-test",
                client_info=m.Implementation(name="agent-relay", version="live-test"),
            ),
        )
        assert init.protocol_version in SUPPORTED_VERSIONS
        raw = await conn.request("listSessions", m.ListSessionsParams(channel="ahp-root://", limit=20))
        listed = m.ListSessionsResult.model_validate(raw)
        assert all(urlparse(item.resource).scheme for item in listed.items)
    finally:
        await conn.close()

    adapter = AhpAdapter(registry)
    healthy, detail = await adapter.health()
    assert "Agent Host" in detail and not healthy
    await adapter.aclose()
