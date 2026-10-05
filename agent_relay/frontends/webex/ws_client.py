# Async port of the websocket client from webex_bot 1.3.1
# (https://github.com/fbradyirl/webex_bot, webex_bot/websockets/webex_websocket_client.py).
#
# MIT License
#
# Copyright (c) 2021, Finbarr Brady
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#
# Changes from the original: asyncio throughout (no run_until_complete, no cross-loop sends),
# httpx instead of requests, stale device clean-up on start, an `on_connected` callback so the
# caller can catch up on missed messages, and injectable connect/sleep for tests.
"""Webex Mercury websocket client: U2C → WDM device → websocket → activities → ack."""

from __future__ import annotations

import asyncio
import json
import socket
import ssl
import uuid
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Literal, Protocol, cast

import certifi
import httpx
import websockets
from pydantic import JsonValue
from websockets.exceptions import ConnectionClosed, InvalidStatus

from agent_relay.log import get_logger

log = get_logger(__name__)

DEVICE_DATA: dict[str, JsonValue] = {
    "deviceName": "agent-relay",
    "deviceType": "DESKTOP",
    "localizedModel": "python",
    "model": "python",
    "name": "agent-relay-relayd",
    "systemName": "agent-relay",
    "systemVersion": "1.0",
}
MAX_BACKOFF_S = 240.0
MAX_404_RETRIES = 3

ActivityVerb = Literal["post", "cardAction"]


@dataclass(frozen=True)
class Activity:
    verb: ActivityVerb
    message_id: str


class WebSocketLike(Protocol):
    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...


type Connector = Callable[[str], AbstractAsyncContextManager[WebSocketLike]]
type ActivityHandler = Callable[[Activity], Awaitable[None]]
type ConnectedHandler = Callable[[], Awaitable[object]]


class DeviceError(RuntimeError):
    pass


def default_connector(url: str) -> AbstractAsyncContextManager[WebSocketLike]:
    context = ssl.create_default_context(cafile=certifi.where())
    return cast(
        AbstractAsyncContextManager[WebSocketLike],
        websockets.connect(url, ssl=context if url.startswith("wss") else None, max_size=2**22),
    )


class WebexWsClient:
    def __init__(
        self,
        token: str,
        http: httpx.AsyncClient,
        u2c_url: str,
        on_activity: ActivityHandler,
        on_connected: ConnectedHandler | None = None,
        connector: Connector = default_connector,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        backoff: tuple[float, float] = (1.0, MAX_BACKOFF_S),
    ) -> None:
        self._token = token
        self._http = http
        self._u2c_url = u2c_url
        self._on_activity = on_activity
        self._on_connected = on_connected
        self._connector = connector
        self._sleep = sleep
        self._backoff_min, self._backoff_max = backoff
        self._device_url: str | None = None
        self._device: dict[str, JsonValue] | None = None
        self._share_id: str | None = None
        self.connections = 0
        self.connected = False
        self.last_error: str | None = None

    async def device_url(self) -> str:
        if self._device_url is None:
            response = await self._http.get(self._u2c_url, params={"format": "hostmap"})
            if response.status_code == 401:
                raise DeviceError("Webex rejected the bot token (401). Check the Keychain entry.")
            links = response.json().get("serviceLinks", {})
            wdm = links.get("wdm") if isinstance(links, dict) else None
            if not isinstance(wdm, str):
                raise DeviceError("U2C catalog has no wdm service link")
            self._device_url = wdm.rstrip("/")
        return self._device_url

    async def cleanup_stale_devices(self) -> int:
        """Delete every device registration this relay created earlier. Returns how many."""
        base = await self.device_url()
        response = await self._http.get(f"{base}/devices")
        removed = 0
        if response.status_code >= 400:
            log.warning(f"WDM device list failed: HTTP {response.status_code}")
            return 0
        devices = response.json().get("devices", [])
        for device in devices if isinstance(devices, list) else []:
            if isinstance(device, dict) and device.get("name") == DEVICE_DATA["name"]:
                url = device.get("url")
                if isinstance(url, str):
                    await self._http.delete(url)
                    removed += 1
        if removed:
            log.info(f"removed {removed} stale WDM device registration(s)")
        return removed

    async def register_device(self) -> dict[str, JsonValue]:
        base = await self.device_url()
        response = await self._http.post(f"{base}/devices", json=DEVICE_DATA)
        if response.status_code >= 400:
            raise DeviceError(f"WDM device registration failed: HTTP {response.status_code}")
        device = response.json()
        if not isinstance(device, dict) or not isinstance(device.get("webSocketUrl"), str):
            raise DeviceError("WDM device has no webSocketUrl")
        self._device = device
        return device

    async def resolve_message_id(self, activity: dict[str, JsonValue]) -> str | None:
        """Find the REST (base64) id. The conversation URL locates the right data centre."""
        verb = activity.get("verb")
        activity_id = activity.get("id")
        target = activity.get("target")
        if not isinstance(target, dict) or not isinstance(activity_id, str):
            return None
        conversation_url = target.get("url")
        conversation_id = target.get("id")
        if not isinstance(conversation_url, str) or not isinstance(conversation_id, str):
            return None
        kind = "messages" if verb in ("post", "update") else "attachment/actions"
        if verb == "update" and self._share_id is not None:
            activity_id, self._share_id = self._share_id, None
        url = conversation_url.replace(f"conversations/{conversation_id}", f"{kind}/{activity_id}")
        try:
            response = await self._http.get(url)
        except httpx.HTTPError as exc:
            log.warning(f"could not resolve activity {activity_id}: {type(exc).__name__}; left to catch-up")
            return None
        if response.status_code >= 400:
            log.warning(f"could not resolve activity {activity_id}: HTTP {response.status_code}")
            return None
        found = response.json()
        resolved = found.get("id") if isinstance(found, dict) else None
        return resolved if isinstance(resolved, str) else None

    async def _activity_from(self, frame: dict[str, JsonValue]) -> Activity | None:
        data = frame.get("data")
        if not isinstance(data, dict) or data.get("eventType") != "conversation.activity":
            return None
        activity = data.get("activity")
        if not isinstance(activity, dict):
            return None
        verb = activity.get("verb")
        if verb == "share":
            share_id = activity.get("id")
            self._share_id = share_id if isinstance(share_id, str) else None
            return None
        if verb == "update" and not _is_safe_file_update(activity):
            return None
        if verb not in ("post", "update", "cardAction"):
            return None
        message_id = await self.resolve_message_id(activity)
        if message_id is None:
            return None
        return Activity(verb="cardAction" if verb == "cardAction" else "post", message_id=message_id)

    async def handle_frame(self, ws: WebSocketLike, raw: str | bytes) -> None:
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("ignoring a non-JSON websocket frame")
            return
        if not isinstance(frame, dict):
            return
        activity = await self._activity_from(frame)
        if activity is None:
            return
        await ws.send(json.dumps({"type": "ack", "messageId": activity.message_id}))
        try:
            await self._on_activity(activity)
        except Exception:
            log.exception(f"activity handler failed for {activity.verb}")

    async def _listen(self, url: str) -> None:
        async with self._connector(url) as ws:
            await ws.send(
                json.dumps(
                    {
                        "id": str(uuid.uuid4()),
                        "type": "authorization",
                        "data": {"token": f"Bearer {self._token}"},
                    }
                )
            )
            self.connections += 1
            self.connected, self.last_error = True, None
            log.info("Webex websocket connected")
            try:
                if self._on_connected is not None:
                    try:
                        await self._on_connected()
                    except Exception:
                        log.exception("catch-up after connecting failed; listening anyway")
                while True:
                    await self.handle_frame(ws, await ws.recv())
            finally:
                self.connected = False

    async def run(self) -> None:
        """Connect and listen forever, reconnecting with exponential backoff.

        Network failures, including HTTP calls to Webex, are retried; a rejected token is not.
        """
        device: dict[str, JsonValue] | None = None
        cleaned = False
        delay = self._backoff_min
        not_found = 0
        while True:
            before = self.connections
            try:
                if not cleaned:
                    await self.cleanup_stale_devices()
                    cleaned = True
                if device is None:
                    device = await self.register_device()
                await self._listen(cast(str, device["webSocketUrl"]))
            except asyncio.CancelledError:
                raise
            except InvalidStatus as exc:
                status = exc.response.status_code
                self.last_error = f"websocket handshake failed: HTTP {status}"
                log.warning(self.last_error)
                if status == 404:
                    not_found += 1
                    if not_found > MAX_404_RETRIES:
                        raise DeviceError("device registration keeps returning 404") from exc
                    device = None
            except (ConnectionClosed, OSError, socket.gaierror, httpx.HTTPError) as exc:
                if self.connections > before:
                    delay = self._backoff_min
                self.last_error = f"websocket dropped ({type(exc).__name__})"
                log.warning(f"{self.last_error}; reconnecting in {delay:.0f}s")
            if self.connections > before:
                delay = self._backoff_min
                not_found = 0
            await self._sleep(delay)
            delay = min(delay * 2, self._backoff_max)


def _is_safe_file_update(activity: dict[str, JsonValue]) -> bool:
    obj = activity.get("object")
    if not isinstance(obj, dict) or obj.get("objectType") != "content":
        return False
    if obj.get("contentCategory") != "documents":
        return False
    files = obj.get("files")
    items = files.get("items") if isinstance(files, dict) else None
    if not isinstance(items, list):
        return False
    return all(isinstance(i, dict) and i.get("malwareQuarantineState") == "safe" for i in items)
