"""AC-16 (live, RELAY_LIVE=1): list sessions on the real VS Code Agent Host.

Read-only: it creates nothing, so it does not use any of the AHP live attempts (all three went to S1).
"""

from __future__ import annotations

from agent_relay.adapters.ahp import models as m
from agent_relay.adapters.ahp.adapter import AhpAdapter
from agent_relay.adapters.ahp.endpoints import discover
from agent_relay.adapters.ahp.rpc import Connection
from agent_relay.core.registry import Registry
from tests.conftest import live

pytestmark = live


async def test_lists_sessions_on_the_editor_host(registry: Registry) -> None:
    endpoint = await discover()
    conn = await Connection.open(endpoint)
    try:
        init = m.InitializeResult.model_validate(
            await conn.request(
                "initialize",
                m.InitializeParams(
                    channel="ahp-root://",
                    protocol_versions=[endpoint.protocol_version],
                    client_id="agent-relay-live-test",
                    client_info=m.Implementation(name="agent-relay", version="live-test"),
                ),
            )
        )
        assert init.protocol_version == endpoint.protocol_version
        raw = await conn.request("listSessions", m.ListSessionsParams(channel="ahp-root://", limit=20))
        listed = m.ListSessionsResult.model_validate(raw)
        assert all(item.resource.startswith("ahp-session:/") for item in listed.items)
    finally:
        await conn.close()

    adapter = AhpAdapter(registry)
    healthy, detail = await adapter.health()
    assert "Agent Host" in detail and not healthy
    await adapter.aclose()
