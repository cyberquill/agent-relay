"""The vendored websocket client against a fake Mercury; AC-9 catch-up after reconnect, exactly once."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import httpx
import pytest
from pydantic import JsonValue
from websockets.exceptions import ConnectionClosedError, InvalidStatus

from agent_relay.frontends.webex.ws_client import DEVICE_DATA, Activity, WebexWsClient, WebSocketLike
from tests.fakes.webex import U2C, FakeWebex
from tests.frontends.conftest import Relay


class FakeSocket:
    def __init__(self, frames: list[str], then: BaseException) -> None:
        self.sent: list[dict[str, JsonValue]] = []
        self._frames = frames
        self._then = then

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    async def recv(self) -> str | bytes:
        if self._frames:
            return self._frames.pop(0)
        raise self._then


class Mercury:
    """Hands out one scripted socket per connection attempt."""

    def __init__(self, scripts: list[FakeSocket | BaseException]) -> None:
        self.scripts = scripts
        self.urls: list[str] = []
        self.sockets: list[FakeSocket] = []

    def connect(self, url: str) -> AbstractAsyncContextManager[WebSocketLike]:
        self.urls.append(url)
        script = self.scripts.pop(0)

        @asynccontextmanager
        async def _cm() -> AsyncIterator[WebSocketLike]:
            if isinstance(script, BaseException):
                raise script
            self.sockets.append(script)
            yield script

        return _cm()


class Stop(BaseException):
    """Ends the client's run loop in tests."""


def _invalid(status: int) -> InvalidStatus:
    return InvalidStatus(httpx.Response(status))  # type: ignore[arg-type]


async def _stop_sleep(_: float) -> None:
    raise Stop


async def test_protocol_steps_auth_ack_and_stale_cleanup() -> None:
    webex = FakeWebex()
    async with webex.client() as http:
        webex.devices["old"] = {"name": DEVICE_DATA["name"], "url": "https://wdm.fake/wdm/api/v1/devices/old"}
        webex.devices["other"] = {"name": "someone-else", "url": "https://wdm.fake/wdm/api/v1/devices/other"}
        message_id = webex.user_says("hi")
        frames = [
            json.dumps(webex.activity_for(message_id)),
            "not json",
            json.dumps({"data": {"eventType": "x"}}),
        ]
        mercury = Mercury([FakeSocket(frames, ConnectionClosedError(None, None))])
        seen: list[Activity] = []

        async def on_activity(activity: Activity) -> None:
            seen.append(activity)

        client = WebexWsClient("tok", http, U2C, on_activity, connector=mercury.connect, sleep=_stop_sleep)
        with pytest.raises(Stop):
            await client.run()
    assert "old" not in webex.devices and "other" in webex.devices
    sock = mercury.sockets[0]
    assert sock.sent[0]["type"] == "authorization"
    assert sock.sent[0]["data"] == {"token": "Bearer tok"}
    assert sock.sent[1] == {"type": "ack", "messageId": message_id}
    assert seen == [Activity("post", message_id)]


async def test_404_refreshes_device_and_backoff_grows() -> None:
    webex = FakeWebex()
    async with webex.client() as http:
        mercury = Mercury(
            [_invalid(404), ConnectionRefusedError(), FakeSocket([], ConnectionClosedError(None, None))]
        )
        delays: list[float] = []

        async def sleep(d: float) -> None:
            delays.append(d)
            if len(delays) >= 3:
                raise Stop

        async def on_activity(_: Activity) -> None:
            return None

        client = WebexWsClient(
            "tok", http, U2C, on_activity, connector=mercury.connect, sleep=sleep, backoff=(1, 8)
        )
        with pytest.raises(Stop):
            await client.run()
    assert len(set(mercury.urls)) == 2
    assert delays == [1, 2, 1]


async def test_a_drop_after_a_good_connection_logs_the_short_delay_it_will_use(
    caplog: pytest.LogCaptureFixture,
) -> None:
    webex = FakeWebex()
    async with webex.client() as http:
        mercury = Mercury(
            [
                ConnectionRefusedError(),
                ConnectionRefusedError(),
                FakeSocket([], ConnectionClosedError(None, None)),
            ]
        )
        delays: list[float] = []

        async def sleep(d: float) -> None:
            delays.append(d)
            if len(delays) >= 3:
                raise Stop

        async def on_activity(_: Activity) -> None:
            return None

        client = WebexWsClient(
            "tok", http, U2C, on_activity, connector=mercury.connect, sleep=sleep, backoff=(1, 240)
        )
        with caplog.at_level("WARNING"), pytest.raises(Stop):
            await client.run()
    assert delays == [1, 2, 1]
    assert [r.getMessage() for r in caplog.records][-1].endswith("reconnecting in 1s")


async def test_repeated_404_gives_up() -> None:
    webex = FakeWebex()
    async with webex.client() as http:
        mercury = Mercury([_invalid(404) for _ in range(5)])

        async def sleep(_: float) -> None:
            return None

        async def on_activity(_: Activity) -> None:
            return None

        client = WebexWsClient("tok", http, U2C, on_activity, connector=mercury.connect, sleep=sleep)
        with pytest.raises(RuntimeError, match="404"):
            await client.run()


async def test_bad_token_is_reported() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:

        async def on_activity(_: Activity) -> None:
            return None

        client = WebexWsClient("tok", http, U2C, on_activity)
        with pytest.raises(RuntimeError, match="401"):
            await client.run()


async def test_waking_from_sleep_retries_http_failures_instead_of_stopping() -> None:
    """After a sleep the network comes back in pieces: none of these may end the client."""
    webex = FakeWebex()
    inner = webex.transport()
    first_u2c, first_lookup = [True], [True]

    async def flaky(request: httpx.Request) -> httpx.Response:
        if request.url.host == "u2c.fake" and first_u2c[0]:
            first_u2c[0] = False
            raise httpx.ConnectError("no route to host", request=request)
        if request.url.host == "conv.fake" and first_lookup[0]:
            first_lookup[0] = False
            raise httpx.ReadTimeout("", request=request)
        return await inner.handle_async_request(request)

    lost, kept = webex.user_says("lost"), webex.user_says("kept")
    frames = [json.dumps(webex.activity_for(lost)), json.dumps(webex.activity_for(kept))]
    mercury = Mercury([FakeSocket(frames, ConnectionClosedError(None, None))])
    seen: list[Activity] = []
    delays: list[float] = []

    async def on_activity(activity: Activity) -> None:
        seen.append(activity)

    async def on_connected() -> None:
        raise httpx.ReadTimeout("")

    async def sleep(d: float) -> None:
        delays.append(d)
        if len(delays) >= 2:
            raise Stop

    async with httpx.AsyncClient(transport=httpx.MockTransport(flaky)) as http:
        client = WebexWsClient(
            "tok",
            http,
            U2C,
            on_activity,
            on_connected,
            connector=mercury.connect,
            sleep=sleep,
            backoff=(1, 8),
        )
        with pytest.raises(Stop):
            await client.run()
    assert client.connections == 1
    assert seen == [Activity("post", kept)]
    assert delays == [1, 1]


async def test_catch_up_after_reconnect_processes_missed_messages_exactly_once(relay: Relay) -> None:
    webex = relay.webex
    first = webex.user_says("/help")
    await relay.frontend.handle_message(await relay.frontend.api.get_message(first))
    missed = [webex.user_says("/ws"), webex.user_says("/apps")]
    live_after = webex.user_says("/sessions")
    frames_second = [json.dumps(webex.activity_for(missed[0])), json.dumps(webex.activity_for(live_after))]
    mercury = Mercury(
        [
            FakeSocket([], ConnectionClosedError(None, None)),
            FakeSocket(frames_second, ConnectionClosedError(None, None)),
        ]
    )
    calls = [0]

    async def sleep(_: float) -> None:
        calls[0] += 1
        if calls[0] >= 2:
            raise Stop

    client = WebexWsClient(
        "tok",
        httpx.AsyncClient(transport=webex.transport()),
        U2C,
        relay.frontend.on_activity,
        relay.frontend.catch_up,
        connector=mercury.connect,
        sleep=sleep,
    )
    with pytest.raises(Stop):
        await client.run()
    await asyncio.sleep(0)
    replies = relay.bot_texts()
    assert sum("agent-relay commands" in r for r in replies) == 1
    assert sum("**proj**" in r for r in replies) == 1
    assert sum("OpenCode" in r for r in replies) == 1
    assert sum(r == "No active sessions." for r in replies) == 1
