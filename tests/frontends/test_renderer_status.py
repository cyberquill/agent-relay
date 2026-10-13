"""The thread's root status line (status, queue count, edit budget) and the turn's progress message."""

from __future__ import annotations

from agent_relay.frontends.webex.api import MAX_EDITS
from agent_relay.frontends.webex.bot import build_frontend
from agent_relay.frontends.webex.renderer import PROGRESS_EDITS, ROOT_POLL_S
from agent_relay.models.events import Harness, SessionStarted, ToolStarted, TurnCompleted
from agent_relay.models.sessions import SessionRecord, SessionStatus
from tests.frontends.conftest import Relay

H = Harness.OPENCODE


async def _session(relay: Relay) -> SessionRecord:
    return await relay.registry.create_session(
        SessionRecord(session_key="opencode:s1", harness=H, harness_id="s1", workspace="/w/proj", model="m1")
    )


async def _status(relay: Relay, session: SessionRecord, status: SessionStatus) -> None:
    await relay.registry.set_session_status(session.session_key, status)


def _root(relay: Relay) -> str:
    return relay.webex.bot_messages()[0].markdown


def _root_edits(relay: Relay) -> int:
    return relay.webex.edits.get(relay.webex.bot_messages()[0].id, 0)


async def _tool(relay: Relay, session: SessionRecord, n: int) -> None:
    await relay.frontend.renderer.handle(
        ToolStarted(harness=H, session_key=session.session_key, tool_id=str(n), name="bash", detail=f"ls {n}")
    )


def _progress_messages(relay: Relay) -> list[str]:
    return [m.id for m in relay.webex.bot_messages() if "🔧 bash" in m.markdown]


# root status line


async def test_the_root_follows_a_turn_and_shows_the_queue(relay: Relay) -> None:
    session = await _session(relay)
    renderer = relay.frontend.renderer
    await renderer.handle(SessionStarted(harness=H, session_key=session.session_key))
    assert "🟢 active" in _root(relay)
    await _status(relay, session, SessionStatus.RUNNING)
    await relay.registry.enqueue(session.session_key, "prompt", "next")
    await relay.registry.enqueue(session.session_key, "prompt", "after that")
    await _tool(relay, session, 0)
    assert "⏳ running · 2 queued · queue" in _root(relay)
    await _status(relay, session, SessionStatus.IDLE)
    await renderer.handle(TurnCompleted(harness=H, session_key=session.session_key, text="done"))
    assert "💤 idle" in _root(relay)


async def test_the_root_names_the_sessions_queue_mode(relay: Relay) -> None:
    session = await _session(relay)
    await relay.service.set_queue_mode(session.session_key, "steer")
    await relay.frontend.renderer.handle(SessionStarted(harness=H, session_key=session.session_key))
    await _status(relay, session, SessionStatus.RUNNING)
    await relay.registry.enqueue(session.session_key, "prompt", "x")
    await _tool(relay, session, 0)
    assert "1 queued · steer" in _root(relay)


async def test_the_daemons_renderer_uses_the_configured_default_queue_mode(relay: Relay) -> None:
    relay.config.queue.default_mode = "interrupt"
    client = relay.webex.client()
    try:
        frontend, _ = await build_frontend("t", relay.config, relay.registry, relay.service, http=client)
        session = await _session(relay)
        await frontend.renderer.handle(SessionStarted(harness=H, session_key=session.session_key))
        await _status(relay, session, SessionStatus.RUNNING)
        await relay.registry.enqueue(session.session_key, "prompt", "x")
        await frontend.renderer.handle(
            ToolStarted(harness=H, session_key=session.session_key, tool_id="0", name="bash", detail="ls")
        )
    finally:
        await client.aclose()
    assert "1 queued · interrupt" in _root(relay)


async def test_a_queue_change_alone_never_edits_the_root(relay: Relay) -> None:
    session = await _session(relay)
    renderer = relay.frontend.renderer
    await renderer.handle(SessionStarted(harness=H, session_key=session.session_key))
    await relay.registry.enqueue(session.session_key, "prompt", "x")
    await renderer.refresh_root(await relay.service.session(session.session_key))
    assert _root_edits(relay) == 0 and "queued" not in _root(relay)


async def test_a_status_change_nobody_announced_reaches_the_root_on_a_tick(relay: Relay) -> None:
    session = await _session(relay)
    renderer = relay.frontend.renderer
    await renderer.handle(SessionStarted(harness=H, session_key=session.session_key))
    await renderer.tick()
    await _status(relay, session, SessionStatus.ENDED)
    await renderer.tick()
    assert "⚫ ended" not in _root(relay)
    relay.clock.now += ROOT_POLL_S
    await renderer.tick()
    assert "⚫ ended" in _root(relay)


async def test_the_last_root_edits_are_kept_for_idle_and_ended(relay: Relay) -> None:
    relay.config.renderer.root_edit_budget = 3
    session = await _session(relay)
    renderer = relay.frontend.renderer
    await renderer.handle(SessionStarted(harness=H, session_key=session.session_key))

    async def move(status: SessionStatus) -> None:
        await _status(relay, session, status)
        await renderer.refresh_root(await relay.service.session(session.session_key))

    await move(SessionStatus.RUNNING)
    await move(SessionStatus.PARKED)
    assert _root_edits(relay) == 2 and "🅿️ parked" in _root(relay)
    await move(SessionStatus.RUNNING)
    assert _root_edits(relay) == 2
    await move(SessionStatus.IDLE)
    assert _root_edits(relay) == 3 and "💤 idle" in _root(relay)


async def test_running_is_skipped_when_only_the_closing_edits_remain(relay: Relay) -> None:
    relay.config.renderer.root_edit_budget = 2
    session = await _session(relay)
    renderer = relay.frontend.renderer
    await renderer.handle(SessionStarted(harness=H, session_key=session.session_key))
    await _status(relay, session, SessionStatus.RUNNING)
    await renderer.refresh_root(await relay.service.session(session.session_key))
    assert _root_edits(relay) == 0
    await _status(relay, session, SessionStatus.IDLE)
    await renderer.refresh_root(await relay.service.session(session.session_key))
    assert _root_edits(relay) == 1 and "💤 idle" in _root(relay)


# progress message


async def test_progress_edits_wait_for_the_interval(relay: Relay) -> None:
    session = await _session(relay)
    renderer = relay.frontend.renderer
    await _tool(relay, session, 0)
    [first] = _progress_messages(relay)
    await _tool(relay, session, 1)
    await renderer.tick()
    assert relay.webex.edits.get(first, 0) == 0
    relay.clock.now += relay.config.renderer.progress_edit_interval_s
    await renderer.tick()
    assert relay.webex.edits[first] == 1
    assert "2 tool calls" in relay.webex.messages[first].markdown


async def test_a_spent_progress_message_is_replaced_by_a_fresh_one(relay: Relay) -> None:
    session = await _session(relay)
    for i in range(PROGRESS_EDITS + 3):
        relay.clock.now += relay.config.renderer.progress_edit_interval_s
        await _tool(relay, session, i)
    first, second = _progress_messages(relay)
    assert relay.webex.edits[first] == PROGRESS_EDITS
    assert relay.webex.edits[second] == 1
    assert relay.webex.violations == []


async def test_the_closing_edit_lands_on_a_message_with_one_edit_left(relay: Relay) -> None:
    session = await _session(relay)
    for i in range(PROGRESS_EDITS + 1):
        relay.clock.now += relay.config.renderer.progress_edit_interval_s
        await _tool(relay, session, i)
    [message] = _progress_messages(relay)
    assert relay.webex.edits[message] == PROGRESS_EDITS
    await relay.frontend.renderer.handle(TurnCompleted(harness=H, session_key=session.session_key, text="ok"))
    assert _progress_messages(relay) == [message]
    assert relay.webex.edits[message] == MAX_EDITS
    assert relay.webex.messages[message].markdown.startswith(f"**✅ Done · {PROGRESS_EDITS + 1} tool calls")
    assert relay.webex.violations == []


async def test_a_closing_edit_that_cannot_land_is_posted_fresh(relay: Relay) -> None:
    session = await _session(relay)
    await _tool(relay, session, 0)
    [message] = _progress_messages(relay)
    room = relay.webex.messages[message].room_id
    for _ in range(MAX_EDITS):  # another client spends the message's edits
        await relay.frontend.api.edit_message(message, room, "⏳ Working · 1 tool call\n🔧 bash: ls 0")
    await relay.frontend.renderer.handle(TurnCompleted(harness=H, session_key=session.session_key, text="ok"))
    done = [m.markdown for m in relay.webex.bot_messages() if m.markdown.startswith("**✅ Done")]
    assert len(done) == 1
