"""`WebexHost`: the stable sink and posting handle, and the supervisor that never lets Webex stop relayd."""

from __future__ import annotations

import asyncio
import logging

import httpx
import pytest
from pydantic import JsonValue

from agent_relay.frontends.webex.api import Message, WebexError
from agent_relay.frontends.webex.host import (
    MAX_BACKOFF_S,
    TOKEN_REJECTED,
    Built,
    HostRenderer,
    PostedFile,
    WebexHost,
    classify,
)
from agent_relay.frontends.webex.ws_client import DeviceError
from agent_relay.models.events import ErrorEvent, Event, Harness
from agent_relay.models.sessions import SessionRecord

SESSION = SessionRecord(session_key="opencode:S1", harness=Harness.OPENCODE, harness_id="S1", workspace="/w")


class FakeRenderer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def handle(self, event: Event) -> None:
        self.calls.append(("handle", event.session_key))

    async def post_top(self, markdown: str, card: dict[str, JsonValue] | None = None) -> Message:
        self.calls.append(("post_top", markdown))
        return Message(id="top-1")

    async def post_in_thread(
        self,
        session: SessionRecord,
        markdown: str,
        card: dict[str, JsonValue] | None = None,
        file: PostedFile | None = None,
    ) -> Message:
        self.calls.append(("post_in_thread", (session.session_key, markdown, file)))
        return Message(id="thread-1")

    async def refresh_root(self, session: SessionRecord) -> None:
        self.calls.append(("refresh_root", session.session_key))

    async def thread_for(self, session: SessionRecord) -> str:
        self.calls.append(("thread_for", session.session_key))
        return "root-1"


class FakeFrontend:
    def __init__(self) -> None:
        self.fake = FakeRenderer()
        self.ticks = 0

    @property
    def renderer(self) -> HostRenderer:
        return self.fake

    async def ticker(self, interval: float = 1.0) -> None:
        self.ticks += 1
        await asyncio.Event().wait()


class FakeSocket:
    """Connects at once, then runs until `end` is called (optionally with an error)."""

    def __init__(self) -> None:
        self.connected = False
        self.connections = 0
        self.last_error: str | None = None
        self._ended: asyncio.Future[Exception | None] = asyncio.get_running_loop().create_future()

    def end(self, error: Exception | None = None) -> None:
        self._ended.set_result(error)

    async def run(self) -> None:
        self.connected = True
        self.connections += 1
        try:
            error = await self._ended
        finally:
            self.connected = False
        if error is not None:
            raise error


class Script:
    """`build()` answers from a script: an exception to raise, or `ok` for a fresh frontend and socket."""

    def __init__(self, *steps: Exception | str) -> None:
        self.steps = list(steps)
        self.built: list[tuple[FakeFrontend, FakeSocket]] = []

    async def __call__(self) -> Built:
        step = self.steps.pop(0) if self.steps else "ok"
        if isinstance(step, Exception):
            raise step
        pair = (FakeFrontend(), FakeSocket())
        self.built.append(pair)
        return pair


class Sleeps:
    def __init__(self) -> None:
        self.seen: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.seen.append(seconds)
        await asyncio.sleep(0)


def host(sleeps: Sleeps | None = None) -> WebexHost:
    return WebexHost(sleep=sleeps or Sleeps(), backoff=(1.0, 8.0), observe_every_s=0.005)


async def until(check: object, timeout: float = 5.0) -> None:
    assert callable(check)
    for _ in range(int(timeout / 0.005)):
        if check():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not met")


def event() -> Event:
    return ErrorEvent(harness=Harness.OPENCODE, session_key="opencode:S1", message="boom")


async def test_everything_is_dropped_silently_while_nothing_is_attached() -> None:
    h = host()
    await h.handle(event())
    await h.refresh_root(SESSION)
    assert await h.post_top("hello") is None
    assert await h.post_in_thread(SESSION, "hello") is None
    assert await h.thread_for(SESSION) is None
    assert h.state == "starting" and not h.attached and h.last_error is None


async def test_the_attached_renderer_gets_every_call() -> None:
    h = host()
    frontend, ws = FakeFrontend(), FakeSocket()
    h.attach(frontend, ws)
    file = ("answer.html", b"<p>x</p>", "text/html")
    await h.handle(event())
    assert (await h.post_top("top")) is not None
    sent = await h.post_in_thread(SESSION, "in thread", file=file)
    assert sent is not None and sent.id == "thread-1"
    await h.refresh_root(SESSION)
    assert await h.thread_for(SESSION) == "root-1"
    assert frontend.fake.calls == [
        ("handle", "opencode:S1"),
        ("post_top", "top"),
        ("post_in_thread", ("opencode:S1", "in thread", file)),
        ("refresh_root", "opencode:S1"),
        ("thread_for", "opencode:S1"),
    ]
    h.detach()
    assert not h.attached
    assert await h.post_top("later") is None


async def test_state_follows_the_websocket_while_attached() -> None:
    h = host()
    ws = FakeSocket()
    h.attach(FakeFrontend(), ws)
    assert (h.state, h.attached) == ("starting", True)
    ws.connected, ws.connections = True, 3
    assert (h.state, h.last_error, h.connections) == ("connected", None, 3)
    ws.connected, ws.last_error = False, "websocket dropped (ConnectionClosed)"
    assert (h.state, h.last_error) == ("unreachable", "websocket dropped (ConnectionClosed)")
    h.detach()
    assert (h.state, h.connections, h.last_error) == (
        "unreachable",
        3,
        "websocket dropped (ConnectionClosed)",
    )


@pytest.mark.parametrize(
    ("error", "state"),
    [
        (WebexError(401, "bad token"), "rejected"),
        (WebexError(403, "forbidden"), "rejected"),
        (DeviceError("Webex rejected the bot token (401). Check the Keychain entry."), "rejected"),
        (WebexError(500, "oops"), "unreachable"),
        (DeviceError("device registration keeps returning 404"), "unreachable"),
        (httpx.ConnectError("no route"), "unreachable"),
        (TimeoutError("slow"), "unreachable"),
    ],
)
def test_failures_are_classified(error: Exception, state: str) -> None:
    assert classify(error)[0] == state
    if state == "rejected":
        assert classify(error)[1] == TOKEN_REJECTED


def test_error_text_never_carries_a_bearer_token() -> None:
    _, text = classify(httpx.ConnectError("failed with Authorization: Bearer abcdefghijklmnop0123456789"))
    assert "abcdefghijklmnop0123456789" not in text


async def test_a_rejected_token_is_retried_with_backoff_and_logged_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sleeps = Sleeps()
    h = host(sleeps)
    script = Script(*(WebexError(401, "bad token") for _ in range(5)), "ok")
    with caplog.at_level(logging.INFO, logger="agent_relay"):
        task = asyncio.create_task(h.run(script))
        await until(lambda: h.attached)
        await until(lambda: h.state == "connected")
        await until(lambda: any("Webex connected" in r.getMessage() for r in caplog.records))
        assert sleeps.seen[:5] == [1.0, 2.0, 4.0, 8.0, 8.0], "doubles, then holds at the maximum"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    rejected = [r for r in caplog.records if "rejected" in r.getMessage()]
    assert len(rejected) == 1, "one log line per state change, not one per retry"
    assert "relay setup token --stdin" in rejected[0].getMessage()
    assert not h.attached


async def test_state_is_rejected_between_attempts_then_connected() -> None:
    h = host()
    task = asyncio.create_task(
        h.run(Script(WebexError(401, "bad token"), WebexError(401, "bad token"), "ok"))
    )
    await until(lambda: h.state == "rejected" or h.attached)
    await until(lambda: h.state == "connected")
    assert h.last_error is None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_a_runtime_failure_detaches_then_rebuilds() -> None:
    sleeps = Sleeps()
    h = host(sleeps)
    script = Script()
    task = asyncio.create_task(h.run(script))
    await until(lambda: h.state == "connected")
    first_frontend, first_ws = script.built[0]
    await h.post_in_thread(SESSION, "while connected")
    first_ws.end(OSError("connection reset"))
    await until(lambda: len(script.built) == 2 and h.state == "connected")
    assert h.attached and h.connections == 1
    assert sleeps.seen[-1] == 1.0, "a session that connected resets the backoff"
    await h.post_in_thread(SESSION, "after the rebuild")
    second_frontend, _ = script.built[1]
    assert [c[0] for c in first_frontend.fake.calls] == ["post_in_thread"]
    assert [c[0] for c in second_frontend.fake.calls] == ["post_in_thread"]
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_events_are_dropped_in_the_gap_between_two_front_ends() -> None:
    h = host()
    script = Script()
    task = asyncio.create_task(h.run(script))
    await until(lambda: h.state == "connected")
    script.built[0][1].end(DeviceError("device registration keeps returning 404"))
    await until(lambda: len(script.built) == 2)
    await until(lambda: h.state == "connected")
    await h.handle(event())
    assert script.built[0][0].fake.calls == []
    assert [c[0] for c in script.built[1][0].fake.calls] == ["handle"]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_a_websocket_that_returns_normally_is_rebuilt_too() -> None:
    h = host()
    script = Script()
    task = asyncio.create_task(h.run(script))
    await until(lambda: h.state == "connected")
    script.built[0][1].end(None)
    await until(lambda: len(script.built) == 2)
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_an_unexpected_exception_is_logged_with_a_traceback_and_does_not_stop_relayd(
    caplog: pytest.LogCaptureFixture,
) -> None:
    h = host()
    with caplog.at_level(logging.ERROR, logger="agent_relay"):
        task = asyncio.create_task(h.run(Script(RuntimeError("bug in a handler"), "ok")))
        await until(lambda: h.state == "connected")
    assert any(r.exc_info for r in caplog.records)
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_the_backoff_never_exceeds_the_maximum() -> None:
    sleeps = Sleeps()
    h = WebexHost(sleep=sleeps, observe_every_s=0.005)
    task = asyncio.create_task(h.run(Script(*(httpx.ConnectError("down") for _ in range(12)), "ok")))
    await until(lambda: h.state == "connected")
    assert max(sleeps.seen) == MAX_BACKOFF_S
    assert sleeps.seen[:3] == [1.0, 2.0, 4.0]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
