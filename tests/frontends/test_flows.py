"""Command flows through the Webex front end against fake adapters (AC-6 for hidden capabilities)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from agent_relay import paths
from agent_relay.core import away
from agent_relay.core.service import STICKY_MODEL_KEY
from agent_relay.frontends.webex import cards
from agent_relay.models.events import Harness
from agent_relay.models.sessions import SessionRecord, SessionStatus
from agent_relay.settings import load_workspaces
from tests.fakes.adapters import CrashingAdapter
from tests.frontends.conftest import Relay


async def test_new_wizard_asks_for_each_missing_piece(relay: Relay) -> None:
    await relay.say("/new")
    msg, card = relay.webex.last_card()
    assert cards.card_fallback(card) == "Which app?"
    await relay.click(msg.id, {"relay": "new", "step": "app", "value": "opencode"})
    msg, card = relay.webex.last_card()
    assert cards.card_fallback(card) == "Which workspace?"
    await relay.click(msg.id, {"relay": "new", "step": "ws", "app": "opencode", "value": "proj"})
    msg, card = relay.webex.last_card()
    assert cards.card_fallback(card) == "Which model?"
    await relay.click(
        msg.id, {"relay": "new", "step": "model", "app": "opencode", "ws": "proj", "value": "m2"}
    )
    msg, card = relay.webex.last_card()
    assert cards.card_fallback(card) == "What should it do?"
    await relay.click(
        msg.id,
        {"relay": "new", "step": "prompt", "app": "opencode", "ws": "proj", "model": "m2", "value": "fix it"},
    )
    started = relay.adapter.started[-1]
    assert started.prompt == "fix it" and started.model == "m2"
    assert started.workspace == load_workspaces().workspaces[0].path
    session = (await relay.service.sessions())[0]
    assert session.webex_thread_id and session.origin.value == "phone"
    assert await relay.service.sticky_model(Harness.OPENCODE) == "m2"


async def test_new_in_one_line_and_default_model(relay: Relay) -> None:
    await relay.say('/new opencode proj default "run the tests"')
    assert relay.adapter.started[-1].model is None
    assert relay.adapter.started[-1].prompt == "run the tests"


async def test_new_takes_the_third_word_as_a_model_only_if_the_app_lists_it(relay: Relay) -> None:
    """`/new local scratch Run the shell …` once started a chat with model `Run` (seen live)."""
    started = []
    for line in ("Run the tests now", "m2 fix it", "hello"):
        await relay.say(f"/new opencode proj {line}")
        assert relay.adapter.started, relay.bot_texts()
        started.append((relay.adapter.started[-1].model, relay.adapter.started[-1].prompt))
    assert started == [
        (None, "Run the tests now"),
        ("m2", "fix it"),
        ("m2", "hello"),  # no model given: the sticky one from the last /new
    ]


async def test_one_line_new_starts_without_asking_and_ignores_an_unlisted_sticky_model(relay: Relay) -> None:
    """A sticky `Run` from a mis-parsed /new must not stick (seen live)."""
    await relay.registry.kv_set(STICKY_MODEL_KEY.format(harness=Harness.OPENCODE.value), "Run")
    await relay.say('/new opencode proj "run the tests"')
    assert relay.adapter.started, relay.bot_texts()
    assert relay.adapter.started[-1].model is None
    assert relay.adapter.started[-1].prompt == "run the tests"


async def test_new_with_unknown_app_or_workspace(relay: Relay) -> None:
    await relay.say('/new nope proj "x"')
    assert "Unknown app `nope`" in relay.bot_texts()[-1]
    await relay.say('/new opencode elsewhere m1 "x"')
    assert "Unknown workspace `elsewhere`" in relay.bot_texts()[-1]


async def _thread(relay: Relay) -> SessionRecord:
    await relay.say('/new opencode proj m1 "start"')
    return (await relay.service.sessions())[0]


async def test_thread_prompt_model_stop_status(relay: Relay) -> None:
    session = await _thread(relay)
    root = session.webex_thread_id
    await relay.say("please also update the docs", parent_id=root)
    assert relay.adapter.prompts[-1] == (session.session_key, "please also update the docs")
    await relay.say("/model m2", parent_id=root)
    assert relay.adapter.models[-1] == (session.session_key, "m2")
    await relay.say("/model", parent_id=root)
    assert relay.bot_texts()[-1] == "Model: `m2`"
    await relay.say("/status", parent_id=root)
    assert "model: m2" in relay.bot_texts()[-1]
    await relay.say("/stop", parent_id=root)
    assert relay.adapter.cancelled == [session.session_key]
    replies = [m for m in relay.webex.bot_messages() if m.parent_id == root]
    assert len(replies) >= 4


async def test_parked_session_gets_prompt_queued(relay: Relay) -> None:
    session = await _thread(relay)
    await relay.registry.set_session_fields(session.session_key, status=SessionStatus.PARKED.value)
    await relay.say("carry on", parent_id=session.webex_thread_id)
    queued = await relay.registry.pop_queued(session.session_key, ("prompt",))
    assert [q.text for q in queued] == ["carry on"]
    assert "parked" in relay.bot_texts()[-1]


async def test_unsupported_command_is_refused_per_capability(relay: Relay) -> None:
    session = await relay.registry.upsert_session(
        SessionRecord(session_key="cursor_cdp:x", harness=Harness.CURSOR_CDP, harness_id="x", workspace="/w")
    )
    relay.router.add_adapter(CrashingAdapter())
    await relay.frontend.renderer.thread_for(session)
    stored = await relay.service.session(session.session_key)
    for command in ("/model m", "/stop"):
        await relay.say(command, parent_id=stored.webex_thread_id)
        assert "does not support" in relay.bot_texts()[-1]


async def test_back_in_a_thread_releases_any_parked_session(relay: Relay) -> None:
    """Relay's stop hook parks; the adapter's capabilities have no say in it."""
    session = await relay.registry.upsert_session(
        SessionRecord(session_key="cursor_cdp:x", harness=Harness.CURSOR_CDP, harness_id="x", workspace="/w")
    )
    relay.router.add_adapter(CrashingAdapter())
    await relay.frontend.renderer.thread_for(session)
    stored = await relay.service.session(session.session_key)
    await relay.say("/back", parent_id=stored.webex_thread_id)
    assert relay.bot_texts()[-1] == "That session is not parked."
    await relay.registry.set_session_fields(session.session_key, status=SessionStatus.PARKED.value)
    await relay.say("/back", parent_id=stored.webex_thread_id)
    assert set(relay.bot_texts()[-2:]) == {"Released.", "▶️ Released (/back)."}
    assert await relay.registry.kv_get(f"release:{session.session_key}") == "/back"


async def test_unknown_thread_and_top_level_hint(relay: Relay) -> None:
    await relay.say("hi")
    assert "Reply inside a session thread" in relay.bot_texts()[-1]
    await relay.say("hi", parent_id="not-a-root")
    assert "not linked to a session" in relay.bot_texts()[-1]


async def test_listings(relay: Relay) -> None:
    await relay.say("/apps")
    assert "OpenCode" in relay.bot_texts()[-1] and "cold_start" in relay.bot_texts()[-1]
    await relay.say("/ws")
    assert "**proj**" in relay.bot_texts()[-1]
    await relay.say("/models opencode")
    assert "`m1` Model One (default)" in relay.bot_texts()[-1]
    await relay.say("/sessions")
    assert relay.bot_texts()[-1] == "No active sessions."
    await relay.say("/doctor")
    assert "✅ OpenCode: fake" in relay.bot_texts()[-1]
    await relay.say("/frob")
    assert "Unknown command `/frob`" in relay.bot_texts()[-1]


async def test_away_global_and_per_thread(relay: Relay) -> None:
    await relay.say("/away on")
    assert await away.is_away(relay.registry, "/anything")
    session = await _thread(relay)
    await relay.say("/away off", parent_id=session.webex_thread_id)
    assert not await away.is_away(relay.registry, session.workspace)
    assert await away.is_away(relay.registry, "/elsewhere")
    await relay.say("/away", parent_id=session.webex_thread_id)
    assert await away.is_away(relay.registry, session.workspace)
    await relay.say("/away off")
    await relay.say("/away")
    assert await away.is_away(relay.registry, "/elsewhere")


async def test_panic_pauses_and_cancels(relay: Relay) -> None:
    session = await _thread(relay)
    await relay.say("/away on")
    await relay.say("/panic")
    assert relay.adapter.cancelled == [session.session_key]
    assert paths.pause_flag().exists()
    assert not await away.is_away(relay.registry, session.workspace)
    assert "Paused" in relay.bot_texts()[-1]
    await relay.say("/away on")
    assert not paths.pause_flag().exists()


async def test_diff_attachment(relay: Relay) -> None:
    workspace = Path(load_workspaces().workspaces[0].path)
    git = ["git", "-C", str(workspace)]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run(
        [*git, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "i"],
        check=True,
    )
    session = await _thread(relay)
    await relay.say("/diff", parent_id=session.webex_thread_id)
    assert relay.bot_texts()[-1] == "No changes."
    (workspace / "a.txt").write_text("one\n")
    subprocess.run([*git, "add", "a.txt"], check=True)
    await relay.say("/diff", parent_id=session.webex_thread_id)
    last = relay.webex.bot_messages()[-1]
    assert last.files == ["changes.diff"] and b"+one" in last.file_bytes
