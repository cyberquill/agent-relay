"""U2: nothing the relay handles is held for approval, so the phone never sees an approval card."""

from __future__ import annotations

from agent_relay.frontends import actions
from agent_relay.frontends.webex import cards
from agent_relay.models.events import ApprovalRequested, ApprovalResolved, Harness, Origin
from agent_relay.models.sessions import SessionRecord
from tests.frontends.conftest import Relay

H = Harness.OPENCODE


async def test_renderer_never_renders_approval_events(relay: Relay) -> None:
    session = await relay.registry.create_session(
        SessionRecord(
            session_key="opencode:a1",
            harness=H,
            harness_id="a1",
            workspace="/w/proj",
            origin=Origin.PHONE,
        )
    )
    renderer = relay.frontend.renderer
    root = await renderer.thread_for(session)
    before = len(relay.webex.bot_messages())
    await renderer.handle(
        ApprovalRequested(
            harness=H, session_key=session.session_key, request_id="r1", tool="bash", detail="rm -rf /"
        )
    )
    await renderer.handle(
        ApprovalResolved(
            harness=H, session_key=session.session_key, request_id="r1", decision="allow", by="auto"
        )
    )
    assert len(relay.webex.bot_messages()) == before
    assert relay.webex.edits.get(root, 0) == 0
    assert not any(m.attachments for m in relay.webex.bot_messages())


async def test_there_is_no_approval_action_and_no_approval_card_builder() -> None:
    assert "approval" not in actions.action_kinds()
    assert not hasattr(cards, "approval")
