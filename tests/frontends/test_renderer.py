"""AC-13: against the fake Webex no message exceeds a limit, no message is edited more than 10 times,
and 429 responses are honoured. A turn is one message: the final answer and a footer; a long or rich
answer is one message with answer.html. Plus pacing, thread timing and the question card's life."""

from __future__ import annotations

import asyncio
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_relay.frontends import card_state
from agent_relay.frontends.webex import cards
from agent_relay.frontends.webex.api import MAX_EDITS, MAX_TEXT_BYTES
from agent_relay.frontends.webex.renderer import Pacer, chunk_text
from agent_relay.models.events import (
    ErrorEvent,
    Harness,
    Origin,
    QuestionAnswered,
    QuestionAsked,
    SessionParked,
    SessionReleased,
    SessionStarted,
    TextDelta,
    ToolFinished,
    ToolStarted,
    TurnCompleted,
)
from agent_relay.models.sessions import PendingRequest, SessionRecord, SessionStatus
from tests.frontends.conftest import FakeClock, Relay

H = Harness.OPENCODE


async def _session(
    relay: Relay,
    key: str = "opencode:s1",
    origin: Origin = Origin.PHONE,
    workspace: str = "/w/proj",
) -> SessionRecord:
    return await relay.registry.create_session(
        SessionRecord(
            session_key=key,
            harness=H,
            harness_id=key.split(":")[1],
            workspace=workspace,
            model="m1",
            origin=origin,
        )
    )


async def _tool(relay: Relay, session: SessionRecord, n: int, ok: bool = True) -> None:
    key = session.session_key
    await relay.frontend.renderer.handle(
        ToolStarted(harness=H, session_key=key, tool_id=str(n), name="bash", detail=f"ls {n}")
    )
    await relay.frontend.renderer.handle(
        ToolFinished(harness=H, session_key=key, tool_id=str(n), name="bash", ok=ok)
    )


async def _say(relay: Relay, session: SessionRecord, text: str) -> None:
    await relay.frontend.renderer.handle(TextDelta(harness=H, session_key=session.session_key, text=text))


async def _ask(relay: Relay, session: SessionRecord, request_id: str, options: tuple[str, ...] = ()) -> None:
    await relay.registry.add_pending(
        PendingRequest(
            request_id=request_id,
            kind="question",
            session_key=session.session_key,
            payload={"source": "adapter"},
            deadline=datetime.now(UTC) + timedelta(minutes=10),
        )
    )
    await relay.frontend.renderer.handle(
        QuestionAsked(
            harness=H,
            session_key=session.session_key,
            request_id=request_id,
            question="Which?",
            options=options,
        )
    )


async def _complete(relay: Relay, session: SessionRecord, text: str | None = None) -> None:
    await relay.frontend.renderer.handle(TurnCompleted(harness=H, session_key=session.session_key, text=text))


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


# one message per turn


async def test_a_turn_posts_nothing_until_it_completes_and_then_one_message(relay: Relay) -> None:
    session = await _session(relay)
    key = session.session_key
    renderer = relay.frontend.renderer
    await renderer.handle(SessionStarted(harness=H, session_key=key))
    for _ in range(100):
        await _say(relay, session, "word " * 10)
    await _tool(relay, session, 1)
    await _say(relay, session, "All done.")
    assert relay.thread_texts() == []
    await _complete(relay, session, "All done.")
    assert relay.thread_texts() == ["All done.\n\n⏱ 0s · 🔧 1"]
    root = relay.webex.bot_messages()[0]
    assert all(m.parent_id == root.id for m in relay.webex.bot_messages()[1:])


async def test_the_final_text_is_the_last_message_not_the_narration(relay: Relay) -> None:
    session = await _session(relay)
    await _say(relay, session, "Let me look at the files.")
    await _tool(relay, session, 1)
    await _say(relay, session, "Now the tests.")
    await _tool(relay, session, 2)
    await _say(relay, session, "Everything passes.")
    await _complete(relay, session)
    assert relay.thread_texts() == ["Everything passes.\n\n⏱ 0s · 🔧 2"]


@pytest.mark.parametrize("harness", list(Harness))
async def test_every_app_shows_only_its_last_message(relay: Relay, harness: Harness) -> None:
    """Whether an app streams a whole turn or hands over its last message, the phone gets the last one."""
    key = f"{harness.value}:last"
    await relay.registry.create_session(
        SessionRecord(
            session_key=key, harness=harness, harness_id="last", workspace="/w/proj", origin=Origin.PHONE
        )
    )
    renderer = relay.frontend.renderer
    await renderer.handle(TextDelta(harness=harness, session_key=key, text="First I will look around."))
    await renderer.handle(ToolStarted(harness=harness, session_key=key, tool_id="1", name="read"))
    await renderer.handle(TextDelta(harness=harness, session_key=key, text="That is the answer."))
    await renderer.handle(TurnCompleted(harness=harness, session_key=key))
    assert relay.thread_texts() == ["That is the answer.\n\n⏱ 0s · 🔧 1"]


async def test_the_apps_final_text_wins_over_what_was_buffered(relay: Relay) -> None:
    session = await _session(relay)
    await _say(relay, session, "streamed narration")
    await _complete(relay, session, "the app's own last message")
    text = relay.thread_texts()[0]
    assert text.startswith("the app's own last message") and "narration" not in text


async def test_a_stop_note_in_the_final_text_is_kept(relay: Relay) -> None:
    session = await _session(relay)
    await _say(relay, session, "partial work")
    await _complete(relay, session, "partial work\n\nStopped from the phone.")
    assert "partial work\n\nStopped from the phone." in relay.thread_texts()[0]


async def test_a_turn_with_no_text_still_ends_with_one_message(relay: Relay) -> None:
    session = await _session(relay)
    await _tool(relay, session, 1)
    await _complete(relay, session)
    assert relay.thread_texts() == ["✅ Done.\n\n⏱ 0s · 🔧 1"]


async def test_the_footer_has_duration_tools_and_changed_files(relay: Relay, tmp_path: Path) -> None:
    git = ["git", "-C", str(tmp_path)]
    subprocess.run([*git, "init", "-q"], check=True)
    (tmp_path / "a.txt").write_text("one\n")
    subprocess.run([*git, "add", "a.txt"], check=True)
    subprocess.run([*git, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "i"], check=True)
    (tmp_path / "a.txt").write_text("one\ntwo\nthree\n")
    session = await _session(relay, workspace=str(tmp_path))
    await relay.frontend.renderer.handle(SessionStarted(harness=H, session_key=session.session_key))
    for n in range(3):
        await _tool(relay, session, n)
    relay.clock.now += 185
    await _complete(relay, session, "Edited it.")
    assert relay.thread_texts() == ["Edited it.\n\n⏱ 3m 5s · 🔧 3 · 📝 1 file (+2 −0)"]


@pytest.mark.parametrize(
    ("args", "line"),
    [
        ((185.0, 14, 4, 120, 8), "⏱ 3m 5s · 🔧 14 · 📝 4 files (+120 −8)"),
        ((59.6, 0, 0, 0, 0), "⏱ 1m"),
        ((7200.0, 1, 1, 1, 0), "⏱ 2h · 🔧 1 · 📝 1 file (+1 −0)"),
        ((None, 0, 0, 0, 0), ""),
        ((None, 2, 0, 0, 0), "🔧 2"),
    ],
)
def test_footer_line_leaves_out_what_has_nothing_to_say(
    args: tuple[float | None, int, int, int, int], line: str
) -> None:
    assert cards.footer_line(*args) == line


async def test_links_in_the_answer_are_neutralised(relay: Relay) -> None:
    session = await _session(relay)
    await _complete(relay, session, "see https://example.com/page for more")
    assert relay.thread_texts()[0].startswith("see `https://example.com/page` for more")


# long and rich answers


async def test_a_long_answer_is_one_message_with_answer_html(relay: Relay) -> None:
    session = await _session(relay)
    big = "x" * (relay.config.renderer.max_message_bytes + 10)
    await _tool(relay, session, 1)
    await _complete(relay, session, big)
    sent = [m for m in relay.webex.bot_messages() if m.parent_id]
    assert len(sent) == 1
    assert sent[0].files == ["answer.html"]
    assert b"<html" in sent[0].file_bytes and b"Content-Security-Policy" in sent[0].file_bytes
    assert sent[0].markdown.endswith("🔧 1") and len(sent[0].markdown.encode()) < 2_000
    assert "answer.md" not in "".join(m.markdown for m in relay.webex.bot_messages())
    assert relay.webex.violations == []


async def test_a_table_goes_out_as_html_even_when_short(relay: Relay) -> None:
    session = await _session(relay)
    await _complete(relay, session, "Results:\n\n| a | b |\n|---|---|\n| 1 | 2 |\n")
    sent = relay.webex.bot_messages()[-1]
    assert sent.files == ["answer.html"] and b"<table" in sent.file_bytes


async def test_the_html_threshold_never_keeps_everything_as_text_in_chunks(relay: Relay) -> None:
    relay.config.renderer.html_threshold = "never"
    session = await _session(relay)
    big = ("a paragraph of words. " * 40 + "\n\n") * 20
    await _tool(relay, session, 1)
    await _complete(relay, session, big)
    sent = [m for m in relay.webex.bot_messages() if m.parent_id]
    assert len(sent) > 1 and all(not m.files for m in sent)
    assert all(len(m.markdown.encode()) <= MAX_TEXT_BYTES for m in sent)
    assert sent[-1].markdown.endswith("🔧 1") and not any("🔧" in m.markdown for m in sent[:-1])


async def test_the_html_threshold_always_attaches_even_a_short_answer(relay: Relay) -> None:
    relay.config.renderer.html_threshold = "always"
    session = await _session(relay)
    await _complete(relay, session, "short")
    assert relay.webex.bot_messages()[-1].files == ["answer.html"]


# errors


async def test_an_error_is_one_line_and_resets_the_turn(relay: Relay) -> None:
    session = await _session(relay)
    for n in range(3):
        await _tool(relay, session, n)
    await _say(relay, session, "half an answer")
    await relay.frontend.renderer.handle(
        ErrorEvent(harness=H, session_key=session.session_key, message="model gone")
    )
    assert relay.thread_texts() == ["⚠️ model gone"]
    await _tool(relay, session, 9)
    await _complete(relay, session, "second turn")
    assert relay.thread_texts()[-1] == "second turn\n\n⏱ 0s · 🔧 1"


async def test_adapter_errors_are_logged_not_posted(relay: Relay) -> None:
    await relay.frontend.renderer.handle(
        ErrorEvent(harness=H, session_key="adapter:opencode", message="adapter down")
    )
    assert relay.webex.bot_messages() == []


async def test_unknown_session_is_not_rendered(relay: Relay) -> None:
    await relay.frontend.renderer.handle(TextDelta(harness=H, session_key="nope", text="x"))
    assert relay.webex.bot_messages() == []


# parking, release and the root


async def test_parking_and_release_post_nothing_but_the_root_shows_them(relay: Relay) -> None:
    session = await _session(relay)
    renderer = relay.frontend.renderer
    await renderer.thread_for(session)
    root = relay.webex.bot_messages()[0]
    await relay.registry.set_session_status(session.session_key, SessionStatus.PARKED)
    await renderer.handle(
        SessionParked(harness=H, session_key=session.session_key, deadline=datetime.now(UTC))
    )
    assert "🅿️ parked" in root.markdown
    await relay.registry.set_session_status(session.session_key, SessionStatus.ACTIVE)
    await renderer.handle(SessionReleased(harness=H, session_key=session.session_key, reason="/back"))
    assert "▶️ active" in root.markdown
    assert relay.thread_texts() == []


async def test_a_phone_session_opens_its_thread_when_it_starts(relay: Relay) -> None:
    session = await _session(relay)
    await relay.frontend.renderer.handle(SessionStarted(harness=H, session_key=session.session_key))
    assert len(relay.webex.bot_messages()) == 1
    assert (await relay.service.session(session.session_key)).webex_thread_id


async def test_a_desk_session_has_no_thread_until_its_first_final_result(relay: Relay) -> None:
    session = await _session(relay, origin=Origin.DESK)
    renderer = relay.frontend.renderer
    await renderer.handle(SessionStarted(harness=H, session_key=session.session_key))
    await _tool(relay, session, 1)
    await _say(relay, session, "working")
    await relay.registry.set_session_status(session.session_key, SessionStatus.RUNNING)
    assert relay.webex.bot_messages() == []
    await relay.registry.set_session_status(session.session_key, SessionStatus.IDLE)
    await _complete(relay, session, "Finished at the desk.")
    root, final = relay.webex.bot_messages()
    assert root.parent_id is None and "✅ idle" in root.markdown
    assert final.parent_id == root.id and final.markdown.startswith("Finished at the desk.")


async def test_a_desk_session_opens_its_thread_for_a_question_or_an_error(relay: Relay) -> None:
    first = await _session(relay, "opencode:a", Origin.DESK)
    second = await _session(relay, "opencode:b", Origin.DESK)
    renderer = relay.frontend.renderer
    await _ask(relay, first, "req-a")
    await renderer.handle(ErrorEvent(harness=H, session_key=second.session_key, message="broke"))
    roots = [m for m in relay.webex.bot_messages() if m.parent_id is None]
    assert len(roots) == 2 and len(relay.webex.bot_messages()) == 4


async def test_the_root_posts_once_and_is_then_only_edited(relay: Relay) -> None:
    session = await _session(relay)
    renderer = relay.frontend.renderer
    await renderer.handle(SessionStarted(harness=H, session_key=session.session_key))
    for turn in range(3):
        await relay.registry.set_session_status(session.session_key, SessionStatus.RUNNING)
        await _tool(relay, session, turn)
        await relay.registry.set_session_status(session.session_key, SessionStatus.IDLE)
        await _complete(relay, session, f"turn {turn}")
    assert [m.parent_id is None for m in relay.webex.bot_messages()].count(True) == 1


# questions


async def test_a_question_card_is_deleted_once_answered(relay: Relay) -> None:
    session = await _session(relay)
    renderer = relay.frontend.renderer
    await _ask(relay, session, "req-1", options=("a", "b"))
    card_message, _ = relay.webex.last_card()
    deleted_before = card_message.deleted
    assert not deleted_before
    await renderer.handle(
        QuestionAnswered(harness=H, session_key=session.session_key, request_id="req-1", answer="a")
    )
    assert card_message.deleted
    assert await card_state.load_offer(relay.registry, "req-1") == []
    assert card_message not in relay.webex.bot_messages()


async def test_a_question_card_that_cannot_be_deleted_is_edited_to_say_how_it_ended(relay: Relay) -> None:
    session = await _session(relay)
    renderer = relay.frontend.renderer
    await _ask(relay, session, "req-1")
    card_message, _ = relay.webex.last_card()
    relay.webex.refuse_delete = True
    await renderer.handle(
        QuestionAnswered(harness=H, session_key=session.session_key, request_id="req-1", answer=None)
    )
    assert not card_message.deleted and card_message.markdown == "⏭️ Skipped"


# limits


async def test_limits_hold_under_a_flood_with_rate_limits(relay: Relay) -> None:
    session = await _session(relay)
    renderer = relay.frontend.renderer
    relay.webex.rate_limit_next = 4
    await renderer.handle(SessionStarted(harness=H, session_key=session.session_key))
    for i in range(400):
        await _say(relay, session, f"line {i} " + "y" * 90 + "\n")
    for status in [SessionStatus.PARKED, SessionStatus.IDLE] * 10:
        await relay.registry.set_session_status(session.session_key, status)
        await renderer.refresh_root(await relay.service.session(session.session_key))
    await _complete(relay, session)
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


async def test_concurrent_first_use_opens_one_thread(relay: Relay) -> None:
    """The /new card and the session's first event both ask for the thread at once (seen live)."""
    session = await _session(relay)
    renderer = relay.frontend.renderer
    first, second = await asyncio.gather(renderer.thread_for(session), renderer.thread_for(session))
    assert first == second
    assert len(relay.webex.bot_messages()) == 1
