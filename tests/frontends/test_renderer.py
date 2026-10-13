"""AC-13: against the fake Webex no message exceeds a limit, no message is edited more than 10 times,
and 429 responses are honoured. Plus buffering, batching, pacing and attachments."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from agent_relay.frontends.webex.api import MAX_EDITS, MAX_TEXT_BYTES
from agent_relay.frontends.webex.renderer import Pacer, chunk_text
from agent_relay.models.events import (
    ErrorEvent,
    Harness,
    SessionParked,
    SessionStarted,
    TextDelta,
    ToolFinished,
    ToolStarted,
    TurnCompleted,
)
from agent_relay.models.sessions import SessionRecord, SessionStatus
from tests.frontends.conftest import FakeClock, Relay

H = Harness.OPENCODE


async def _session(relay: Relay, key: str = "opencode:s1") -> SessionRecord:
    return await relay.registry.create_session(
        SessionRecord(
            session_key=key, harness=H, harness_id=key.split(":")[1], workspace="/w/proj", model="m1"
        )
    )


def test_chunk_text_respects_limit_and_boundaries() -> None:
    text = ("para one " * 50 + "\n\n") * 30 + "é" * 4000
    pieces = chunk_text(text, 1000)
    assert all(len(p.encode()) <= 1000 for p in pieces)
    assert "".join(pieces).replace("\n", "").replace(" ", "") == text.replace("\n", "").replace(" ", "")


async def test_pacer_spaces_messages() -> None:
    clock = FakeClock()
    pacer = Pacer(2.0, clock, clock.sleep)
    for _ in range(3):
        await pacer.wait("t")
    assert clock.slept == [2.0, 2.0]
    await pacer.wait("other")
    assert clock.slept == [2.0, 2.0]


async def test_streamed_text_is_buffered_and_flushed_at_paragraphs(relay: Relay) -> None:
    session = await _session(relay)
    renderer = relay.frontend.renderer
    await renderer.handle(SessionStarted(harness=H, session_key=session.session_key))
    for _ in range(100):
        await renderer.handle(TextDelta(harness=H, session_key=session.session_key, text="word " * 10))
    await renderer.handle(TextDelta(harness=H, session_key=session.session_key, text="\n\nnext para"))
    posts_before_end = len(relay.webex.bot_messages())
    await renderer.handle(TurnCompleted(harness=H, session_key=session.session_key, text="ignored, streamed"))
    texts = relay.bot_texts()
    assert posts_before_end >= 2
    assert texts[-2] == "next para"
    assert texts[-1].startswith("✅ Turn complete")
    assert "ignored, streamed" not in "".join(texts)
    for msg in relay.webex.bot_messages()[1:]:
        assert msg.parent_id == relay.webex.bot_messages()[0].id


async def test_idle_flush_after_quiet_period(relay: Relay) -> None:
    session = await _session(relay)
    renderer = relay.frontend.renderer
    await renderer.handle(TextDelta(harness=H, session_key=session.session_key, text="short"))
    await renderer.tick()
    assert "short" not in relay.bot_texts()
    relay.clock.now += relay.config.renderer.flush_idle_s + 1
    await renderer.tick()
    assert relay.bot_texts()[-1] == "short"


async def test_tool_events_share_one_progress_message(relay: Relay) -> None:
    session = await _session(relay)
    renderer = relay.frontend.renderer
    for i in range(5):
        await renderer.handle(
            ToolStarted(
                harness=H, session_key=session.session_key, tool_id=str(i), name="bash", detail=f"ls {i}"
            )
        )
    await renderer.handle(
        ToolFinished(
            harness=H, session_key=session.session_key, tool_id="4", name="bash", ok=False, summary="exit 1"
        )
    )
    await renderer.handle(TurnCompleted(harness=H, session_key=session.session_key, text="done"))
    texts = relay.bot_texts()
    progress = [t for t in texts if "🔧 bash" in t]
    assert len(progress) == 1
    assert progress[0].count("\n") == 6 and "❌ bash: exit 1" in progress[0]
    assert progress[0].splitlines()[0] == "**✅ Done · 5 tool calls · 0s**"
    assert texts[-2] == "done"


async def test_large_final_answer_becomes_attachment(relay: Relay) -> None:
    session = await _session(relay)
    big = "x" * (relay.config.renderer.max_message_bytes + 10)
    await relay.frontend.renderer.handle(TurnCompleted(harness=H, session_key=session.session_key, text=big))
    last = relay.webex.bot_messages()[-2]
    assert last.files == ["answer.md"] and last.file_bytes == big.encode()


async def test_limits_hold_under_a_flood_with_rate_limits(relay: Relay) -> None:
    session = await _session(relay)
    renderer = relay.frontend.renderer
    relay.webex.rate_limit_next = 4
    for i in range(400):
        await renderer.handle(
            TextDelta(harness=H, session_key=session.session_key, text=f"line {i} " + "y" * 90 + "\n")
        )
        if i % 50 == 0:
            await renderer.handle(TextDelta(harness=H, session_key=session.session_key, text="\n\n"))
    for status in [SessionStatus.PARKED, SessionStatus.IDLE] * 10:
        await relay.registry.set_session_status(session.session_key, status)
        await renderer.refresh_root(await relay.service.session(session.session_key))
    await renderer.handle(TurnCompleted(harness=H, session_key=session.session_key))
    assert relay.webex.violations == []
    assert relay.webex.rate_limited == 4
    assert relay.clock.slept.count(3.0) == 4
    assert all(len(m.markdown.encode()) <= MAX_TEXT_BYTES for m in relay.webex.bot_messages())
    assert all(n <= MAX_EDITS for n in relay.webex.edits.values())
    root = relay.webex.bot_messages()[0].id
    assert relay.webex.edits[root] == relay.config.renderer.root_edit_budget


async def test_root_edit_budget_persists(relay: Relay) -> None:
    session = await _session(relay)
    renderer = relay.frontend.renderer
    await renderer.thread_for(session)
    await relay.registry.set_session_status(session.session_key, SessionStatus.PARKED)
    await renderer.refresh_root(await relay.service.session(session.session_key))
    stored = await relay.service.session(session.session_key)
    assert stored.extra["root_edits"] == 1 and stored.webex_thread_id


async def test_parked_and_errors_render(relay: Relay) -> None:
    session = await _session(relay)
    renderer = relay.frontend.renderer
    await renderer.handle(
        SessionParked(harness=H, session_key=session.session_key, deadline=datetime.now(UTC))
    )
    await renderer.handle(ErrorEvent(harness=H, session_key=session.session_key, message="model gone"))
    await renderer.handle(ErrorEvent(harness=H, session_key="adapter:opencode", message="adapter down"))
    texts = relay.bot_texts()
    assert any("Parked" in t for t in texts)
    assert "⚠️ model gone" in texts and "⚠️ adapter down" in texts
    adapter_msg = relay.webex.bot_messages()[-1]
    assert adapter_msg.parent_id is None


async def test_unknown_session_is_not_rendered(relay: Relay) -> None:
    await relay.frontend.renderer.handle(TextDelta(harness=H, session_key="nope", text="x"))
    assert relay.webex.bot_messages() == []


async def test_concurrent_first_use_opens_one_thread(relay: Relay) -> None:
    """The /new wizard and the session's first event both ask for the thread at once (seen live)."""
    session = await _session(relay)
    renderer = relay.frontend.renderer
    first, second = await asyncio.gather(renderer.thread_for(session), renderer.thread_for(session))
    assert first == second
    assert len(relay.webex.bot_messages()) == 1
