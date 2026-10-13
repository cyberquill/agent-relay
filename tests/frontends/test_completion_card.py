"""The card that ends a turn: its stats and files, and what its Diff, Continue, Back and Send buttons do."""

from __future__ import annotations

import subprocess
from pathlib import Path

from pydantic import JsonValue

from agent_relay.core.service.queue import HOOKED
from agent_relay.frontends import card_state
from agent_relay.models.events import Harness, TurnCompleted
from agent_relay.models.sessions import SessionRecord, SessionStatus
from agent_relay.settings import load_workspaces
from tests.frontends.card_tools import actions_of, input_by_id
from tests.frontends.conftest import Relay, submit_data

KEY = "opencode:fake1"


def _git(workspace: Path) -> list[str]:
    git = ["git", "-C", str(workspace)]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run(
        [*git, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "i"],
        check=True,
    )
    return git


async def _finish_a_turn(relay: Relay, *, changed: bool = False) -> tuple[str, dict[str, JsonValue]]:
    """Start a session, let its turn complete, and return the completion card."""
    workspace = Path(load_workspaces().workspaces[0].path)
    git = _git(workspace)
    if changed:
        (workspace / "a.txt").write_text("one\n")
        subprocess.run([*git, "add", "a.txt"], check=True)
    await relay.say('/new opencode proj m1 "go"')
    await relay.registry.set_session_status(KEY, SessionStatus.IDLE)
    await relay.frontend.renderer.handle(
        TurnCompleted(harness=Harness.OPENCODE, session_key=KEY, text="done")
    )
    message, card = relay.webex.last_card()
    return message.id, card


def _titles(card: dict[str, JsonValue]) -> list[str]:
    return [str(a["title"]) for a in actions_of(card)]


async def _session(relay: Relay) -> SessionRecord:
    return await relay.service.session(KEY)


async def test_the_card_shows_the_turns_cost_and_changes(relay: Relay) -> None:
    _, card = await _finish_a_turn(relay, changed=True)
    message, _ = relay.webex.last_card()
    assert message.markdown.startswith("✅ Turn complete · ")
    assert "📝 1 file (+1 −0)" in message.markdown and "🔧 0 tool calls" in message.markdown
    assert "a.txt  +1 −0" in str(card)
    assert message.parent_id == (await _session(relay)).webex_thread_id


async def test_the_card_offers_only_the_buttons_that_work_here(relay: Relay) -> None:
    _, card = await _finish_a_turn(relay)
    assert _titles(card) == ["Diff", "Continue"]


async def test_back_and_a_reply_box_appear_for_a_hooked_app_with_quick_reply(relay: Relay) -> None:
    relay.config.renderer.quick_reply = True
    await relay.say('/new opencode proj m1 "go"')
    await relay.registry.set_session_extra(KEY, HOOKED, True)
    await relay.registry.set_session_status(KEY, SessionStatus.IDLE)
    await relay.frontend.renderer.handle(
        TurnCompleted(harness=Harness.OPENCODE, session_key=KEY, text="done")
    )
    _, card = relay.webex.last_card()
    assert _titles(card) == ["Diff", "Continue", "Back", "Send"]
    assert input_by_id(card, "reply")["isMultiline"] is True


async def test_diff_sends_the_changes_into_the_thread_and_leaves_the_other_buttons(relay: Relay) -> None:
    msg, card = await _finish_a_turn(relay, changed=True)
    await relay.tap(msg, card, "Diff")
    sent = relay.webex.bot_messages()[-1]
    assert sent.files == ["changes.diff"] and b"+one" in sent.file_bytes
    assert sent.parent_id == (await _session(relay)).webex_thread_id
    await relay.tap(msg, card, "Continue")
    assert relay.adapter.prompts[-1] == (KEY, "continue")


async def test_diff_without_changes_says_so(relay: Relay) -> None:
    msg, card = await _finish_a_turn(relay)
    await relay.tap(msg, card, "Diff")
    assert relay.bot_texts()[-1] == "No changes."


async def test_continue_starts_the_next_turn_once(relay: Relay) -> None:
    msg, card = await _finish_a_turn(relay)
    await relay.tap(msg, card, "Continue")
    await relay.tap(msg, card, "Continue")
    assert relay.adapter.prompts == [(KEY, "continue")]
    assert relay.bot_texts()[-1] == card_state.EXPIRED


async def test_back_on_a_session_that_is_not_parked_says_so(relay: Relay) -> None:
    relay.config.renderer.quick_reply = True
    await relay.say('/new opencode proj m1 "go"')
    await relay.registry.set_session_extra(KEY, HOOKED, True)
    await relay.registry.set_session_status(KEY, SessionStatus.IDLE)
    await relay.frontend.renderer.handle(
        TurnCompleted(harness=Harness.OPENCODE, session_key=KEY, text="done")
    )
    msg, card = relay.webex.last_card()
    await relay.tap(msg.id, card, "Back")
    assert relay.bot_texts()[-1] == "That session is not parked."


async def test_send_delivers_the_typed_reply(relay: Relay) -> None:
    relay.config.renderer.quick_reply = True
    msg, card = await _finish_a_turn(relay)
    await relay.tap(msg, card, "Send", reply="  use pytest  ")
    assert relay.adapter.prompts[-1] == (KEY, "use pytest")


async def test_send_refuses_an_empty_reply_and_the_button_stays_usable(relay: Relay) -> None:
    relay.config.renderer.quick_reply = True
    msg, card = await _finish_a_turn(relay)
    await relay.tap(msg, card, "Send", reply="   ")
    assert relay.bot_texts()[-1] == "⚠️ Type a reply first."
    assert relay.adapter.prompts == []
    await relay.tap(msg, card, "Send", reply="now")
    assert relay.adapter.prompts[-1] == (KEY, "now")


async def test_each_button_has_its_own_nonce(relay: Relay) -> None:
    _, card = await _finish_a_turn(relay)
    nonces = {str(submit_data(card, t)["nonce"]) for t in ("Diff", "Continue")}
    assert len(nonces) == 2
