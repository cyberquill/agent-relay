"""Handlers run as tracked tasks: serial per thread, `/panic` and `/stop` never queued, and a card tap
or message that failed before it was handled is not lost."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

import httpx
import pytest
from pydantic import JsonValue

from agent_relay.core.registry import SEEN_PREFIX
from agent_relay.frontends.webex.api import AttachmentAction, Room, WebexError
from agent_relay.frontends.webex.handlers import Context
from agent_relay.frontends.webex.ws_client import Activity
from agent_relay.models.sessions import SessionRecord
from tests.frontends.conftest import Relay


async def until(condition: Callable[[], bool], timeout: float = 2.0) -> None:
    for _ in range(int(timeout / 0.005)):
        if condition():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("timed out waiting")


class Gated:
    """Wraps `Handlers.text`: texts in `slow` wait for their gate before running."""

    def __init__(self, relay: Relay, monkeypatch: pytest.MonkeyPatch, slow: set[str]) -> None:
        self.started: list[str] = []
        self.done: list[str] = []
        self.cancelled: list[str] = []
        self.gates = {text: asyncio.Event() for text in slow}
        real = relay.frontend.handlers.text

        async def text(ctx: Context, text: str) -> None:
            self.started.append(text)
            try:
                if text in self.gates:
                    await self.gates[text].wait()
            except asyncio.CancelledError:
                self.cancelled.append(text)
                raise
            await real(ctx, text)
            self.done.append(text)

        monkeypatch.setattr(relay.frontend.handlers, "text", text)


async def _arrive(relay: Relay, text: str, parent_id: str | None = None) -> None:
    await relay.frontend.on_activity(Activity("post", relay.webex.user_says(text, parent_id=parent_id)))


async def _thread(relay: Relay) -> SessionRecord:
    await relay.say('/new opencode proj m1 "start"')
    return (await relay.service.sessions())[0]


async def test_on_activity_returns_before_the_handler_finishes(
    relay: Relay, monkeypatch: pytest.MonkeyPatch
) -> None:
    gated = Gated(relay, monkeypatch, {"/doctor"})
    await _arrive(relay, "/doctor")
    await until(lambda: gated.started == ["/doctor"])
    assert gated.done == []
    gated.gates["/doctor"].set()
    await relay.frontend.drain()
    assert gated.done == ["/doctor"]
    assert "✅ OpenCode: fake" in relay.bot_texts()[-1]


async def test_two_messages_in_one_thread_run_in_order(relay: Relay, monkeypatch: pytest.MonkeyPatch) -> None:
    session = await _thread(relay)
    gated = Gated(relay, monkeypatch, {"first"})
    await _arrive(relay, "first", session.webex_thread_id)
    await _arrive(relay, "second", session.webex_thread_id)
    await until(lambda: gated.started == ["first"])
    await asyncio.sleep(0.05)
    assert gated.started == ["first"]
    gated.gates["first"].set()
    await relay.frontend.drain()
    assert gated.done == ["first", "second"]


async def test_a_busy_thread_does_not_hold_up_other_threads_or_the_top_level(
    relay: Relay, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = await _thread(relay)
    gated = Gated(relay, monkeypatch, {"first"})
    await _arrive(relay, "first", session.webex_thread_id)
    await until(lambda: gated.started == ["first"])
    await _arrive(relay, "/ws")
    await until(lambda: "/ws" in gated.done)
    assert "first" not in gated.done
    gated.gates["first"].set()
    await relay.frontend.drain()


async def test_panic_runs_at_once_even_when_the_top_level_is_busy(
    relay: Relay, monkeypatch: pytest.MonkeyPatch
) -> None:
    gated = Gated(relay, monkeypatch, {"/doctor"})
    await _arrive(relay, "/doctor")
    await until(lambda: gated.started == ["/doctor"])
    await _arrive(relay, "/panic now")
    await until(lambda: any("Paused" in t for t in relay.bot_texts()))
    assert gated.done == ["/panic now"]
    gated.gates["/doctor"].set()
    await relay.frontend.drain()


async def test_stop_runs_at_once_even_when_its_thread_is_busy(
    relay: Relay, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = await _thread(relay)
    gated = Gated(relay, monkeypatch, {"first"})
    await _arrive(relay, "first", session.webex_thread_id)
    await until(lambda: gated.started == ["first"])
    await _arrive(relay, "/stop", session.webex_thread_id)
    await until(lambda: relay.adapter.cancelled == [session.session_key])
    assert "first" not in gated.done
    gated.gates["first"].set()
    await relay.frontend.drain()


async def test_a_failing_handler_is_logged_and_does_not_stop_the_next(
    relay: Relay, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.ERROR)
    calls: list[str] = []

    async def text(ctx: Context, text: str) -> None:
        calls.append(text)
        if text == "boom":
            raise RuntimeError("it broke")

    monkeypatch.setattr(relay.frontend.handlers, "text", text)
    await _arrive(relay, "boom")
    await _arrive(relay, "after")
    await relay.frontend.drain()
    assert calls == ["boom", "after"]
    assert "webex-message failed: RuntimeError: it broke" in caplog.text


async def test_closing_cancels_outstanding_handlers(relay: Relay, monkeypatch: pytest.MonkeyPatch) -> None:
    gated = Gated(relay, monkeypatch, {"/doctor"})
    await _arrive(relay, "/doctor")
    await until(lambda: gated.started == ["/doctor"])
    await relay.frontend.aclose()
    assert gated.cancelled == ["/doctor"]
    assert gated.done == []


async def _card_tap(relay: Relay) -> AttachmentAction:
    """A tap on the first `/new` card, as Webex would deliver it."""
    await relay.say("/new")
    msg, _ = relay.webex.last_card()
    inputs: dict[str, JsonValue] = {
        "relay": "new",
        "step": "app",
        "value": "opencode",
        "nonce": await relay.nonce(),
    }
    return await relay.frontend.api.get_attachment_action(relay.webex.user_clicks(msg.id, inputs))


async def test_a_tap_whose_room_lookup_fails_once_is_retried(
    relay: Relay, monkeypatch: pytest.MonkeyPatch
) -> None:
    action = await _card_tap(relay)
    real = relay.frontend.api.get_room
    failures = [httpx.ConnectError("network down")]

    async def flaky(room_id: str) -> Room:
        if failures:
            raise failures.pop()
        return await real(room_id)

    monkeypatch.setattr(relay.frontend.api, "get_room", flaky)
    waits: list[float] = []

    async def record(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(relay.frontend, "_sleep", record)
    await relay.frontend.on_activity(Activity("cardAction", action.id))
    await relay.frontend.drain()
    assert waits == [1.0]
    assert sum(1 for m in relay.webex.bot_messages() if m.attachments) == 2


async def test_a_tap_that_could_not_be_looked_up_is_not_marked_as_seen(
    relay: Relay, monkeypatch: pytest.MonkeyPatch
) -> None:
    action = await _card_tap(relay)

    async def down(room_id: str) -> Room:
        raise httpx.ConnectError("network down")

    real = relay.frontend.api.get_room
    monkeypatch.setattr(relay.frontend.api, "get_room", down)
    with pytest.raises(httpx.ConnectError):
        await relay.frontend.handle_action(action)
    assert await relay.registry.kv_get(f"{SEEN_PREFIX}{action.id}") is None
    monkeypatch.setattr(relay.frontend.api, "get_room", real)
    await relay.frontend.handle_action(action)
    assert sum(1 for m in relay.webex.bot_messages() if m.attachments) == 2


async def test_a_client_error_is_not_retried(relay: Relay, monkeypatch: pytest.MonkeyPatch) -> None:
    action = await _card_tap(relay)
    calls = [0]

    async def forbidden(room_id: str) -> Room:
        calls[0] += 1
        raise WebexError(403, "no")

    monkeypatch.setattr(relay.frontend.api, "get_room", forbidden)
    with pytest.raises(WebexError):
        await relay.frontend.handle_action(action)
    assert calls == [1]


async def test_a_message_that_failed_before_it_was_handled_is_handled_when_it_comes_again(
    relay: Relay, monkeypatch: pytest.MonkeyPatch
) -> None:
    message = await relay.frontend.api.get_message(relay.webex.user_says("/ws"))

    async def down(*_: object) -> None:
        raise httpx.ConnectError("network down")

    real = relay.frontend._access.check_message
    monkeypatch.setattr(relay.frontend._access, "check_message", down)
    with pytest.raises(httpx.ConnectError):
        await relay.frontend.handle_message(message)
    assert await relay.registry.kv_get(f"{SEEN_PREFIX}{message.id}") is None
    monkeypatch.setattr(relay.frontend._access, "check_message", real)
    await relay.frontend.handle_message(message)
    assert "**proj**" in relay.bot_texts()[-1]
    await relay.frontend.handle_message(message)
    assert sum("**proj**" in t for t in relay.bot_texts()) == 1
