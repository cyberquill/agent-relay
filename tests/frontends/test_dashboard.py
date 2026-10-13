"""`/sessions` as a card: pick a session, then Status, Diff, Stop, Back or Away; each result lands in that
session's thread."""

from __future__ import annotations

import subprocess
from pathlib import Path

from pydantic import JsonValue

from agent_relay.core import away
from agent_relay.frontends import card_state
from agent_relay.frontends.webex import cards
from agent_relay.models.events import Harness
from agent_relay.models.sessions import SessionRecord, SessionStatus
from agent_relay.settings import load_workspaces
from tests.fakes.adapters import CrashingAdapter
from tests.frontends.card_tools import choice_titles, choice_values, input_by_id
from tests.frontends.conftest import Relay

ONE, TWO = "opencode:fake1", "opencode:fake2"


async def _two_sessions(relay: Relay) -> tuple[SessionRecord, SessionRecord]:
    await relay.say('/new opencode proj m1 "one"')
    await relay.say('/new opencode proj m2 "two"')
    return await relay.service.session(ONE), await relay.service.session(TWO)


async def _dashboard(relay: Relay) -> tuple[str, dict[str, JsonValue]]:
    await relay.say("/sessions")
    msg, card = relay.webex.last_card()
    return msg.id, card


def last_reply(relay: Relay) -> tuple[str, str | None]:
    last = relay.webex.bot_messages()[-1]
    return last.markdown, last.parent_id


async def test_sessions_is_a_card_with_one_choice_per_session_and_five_buttons(relay: Relay) -> None:
    await _two_sessions(relay)
    await relay.say("/sessions")
    msg, card = relay.webex.last_card()
    assert msg.markdown == "Sessions (2)"
    picker = input_by_id(card, "session")
    assert picker["style"] == "compact" and set(choice_values(picker)) == {ONE, TWO}
    titles = choice_titles(picker)
    assert all("🟩" in t and "⏳" in t for t in titles)
    actions = card["actions"]
    assert isinstance(actions, list)
    assert [a["title"] for a in actions if isinstance(a, dict)] == ["Status", "Diff", "Stop", "Back", "Away"]


async def test_no_sessions_is_plain_text(relay: Relay) -> None:
    await relay.say("/sessions")
    assert relay.bot_texts()[-1] == "No active sessions."
    assert not relay.webex.bot_messages()[-1].attachments


async def test_status_replies_in_the_chosen_sessions_thread(relay: Relay) -> None:
    first, second = await _two_sessions(relay)
    msg, card = await _dashboard(relay)
    await relay.tap(msg, card, "Status", session=TWO)
    text, parent = last_reply(relay)
    assert "model: m2" in text and parent == second.webex_thread_id != first.webex_thread_id


async def test_stop_cancels_that_session_only(relay: Relay) -> None:
    first, _ = await _two_sessions(relay)
    msg, card = await _dashboard(relay)
    await relay.tap(msg, card, "Stop", session=ONE)
    assert relay.adapter.cancelled == [ONE]
    assert last_reply(relay) == ("⏹️ Stopped.", first.webex_thread_id)


async def test_stop_on_an_app_that_cannot_cancel_is_refused(relay: Relay) -> None:
    await relay.registry.create_session(
        SessionRecord(session_key="cursor_cdp:x", harness=Harness.CURSOR_CDP, harness_id="x", workspace="/w")
    )
    relay.router.add_adapter(CrashingAdapter())
    msg, card = await _dashboard(relay)
    await relay.tap(msg, card, "Stop", session="cursor_cdp:x")
    assert "does not support cancel" in relay.bot_texts()[-1]


async def test_diff_is_an_upload_in_the_thread(relay: Relay) -> None:
    workspace = Path(load_workspaces().workspaces[0].path)
    git = ["git", "-C", str(workspace)]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run(
        [*git, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "i"],
        check=True,
    )
    first, _ = await _two_sessions(relay)
    msg, card = await _dashboard(relay)
    await relay.tap(msg, card, "Diff", session=ONE)
    assert last_reply(relay) == ("No changes.", first.webex_thread_id)
    (workspace / "a.txt").write_text("one\n")
    subprocess.run([*git, "add", "a.txt"], check=True)
    msg, card = await _dashboard(relay)
    await relay.tap(msg, card, "Diff", session=ONE)
    sent = relay.webex.bot_messages()[-1]
    assert sent.files == ["changes.diff"] and b"+one" in sent.file_bytes
    assert sent.parent_id == first.webex_thread_id


async def test_back_releases_a_parked_session(relay: Relay) -> None:
    first, _ = await _two_sessions(relay)
    msg, card = await _dashboard(relay)
    await relay.tap(msg, card, "Back", session=ONE)
    assert last_reply(relay) == ("That session is not parked.", first.webex_thread_id)
    msg, card = await _dashboard(relay)
    await relay.registry.set_session_status(ONE, SessionStatus.PARKED)
    await relay.tap(msg, card, "Back", session=ONE)
    assert await relay.registry.kv_get(f"release:{ONE}") == "/back"


async def test_away_turns_away_on_for_that_sessions_workspace(relay: Relay) -> None:
    first, _ = await _two_sessions(relay)
    msg, card = await _dashboard(relay)
    assert not await away.is_away(relay.registry, first.workspace)
    await relay.tap(msg, card, "Away", session=ONE)
    assert await away.is_away(relay.registry, first.workspace)
    text, parent = last_reply(relay)
    assert text.startswith("Away mode on") and parent == first.webex_thread_id


async def test_a_session_that_is_gone_is_refused_and_the_card_stays_usable(relay: Relay) -> None:
    await _two_sessions(relay)
    msg, card = await _dashboard(relay)
    await relay.tap(msg, card, "Status", session="opencode:gone")
    assert "no longer known" in relay.bot_texts()[-1]
    await relay.tap(msg, card, "Status", session=ONE)
    assert "model: m1" in relay.bot_texts()[-1]


async def test_a_missing_choice_and_an_unknown_op_are_refused(relay: Relay) -> None:
    await _two_sessions(relay)
    msg, card = await _dashboard(relay)
    await relay.tap(msg, card, "Status", session="")
    assert relay.bot_texts()[-1] == "⚠️ Pick a session first."
    await relay.tap(msg, card, "Status", op="explode")
    assert "not an action this card offers" in relay.bot_texts()[-1]
    assert relay.adapter.cancelled == []


async def test_the_dashboard_is_single_use(relay: Relay) -> None:
    await _two_sessions(relay)
    msg, card = await _dashboard(relay)
    await relay.tap(msg, card, "Status", session=ONE)
    await relay.tap(msg, card, "Stop", session=ONE)
    assert relay.adapter.cancelled == []
    assert relay.bot_texts()[-1] == card_state.EXPIRED


def test_a_long_session_list_is_capped_and_says_so() -> None:
    sessions = [
        SessionRecord(
            session_key=f"opencode:s{i}", harness=Harness.OPENCODE, harness_id=f"s{i}", workspace=f"/w/p{i}"
        )
        for i in range(40)
    ]
    card = cards.dashboard(sessions, nonce="n")
    assert len(choice_values(input_by_id(card, "session"))) == cards.MAX_CHOICES
    assert "Showing 25 of 40" in str(card)
