"""JSON-RPC 2.0 over the Agent Host WebSocket (one message per text frame, token in `tkn`)."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from urllib.parse import quote

from pydantic import JsonValue
from websockets.asyncio.client import ClientConnection, unix_connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from agent_relay.adapters.ahp.endpoints import AhpUnavailableError, Endpoint
from agent_relay.adapters.ahp.models import AhpModel
from agent_relay.log import get_logger

log = get_logger(__name__)

REQUEST_TIMEOUT_S = 30.0
MAX_FRAME = 2**24
METHOD_NOT_FOUND = -32601


class AhpConnectionError(AhpUnavailableError):
    """The Agent Host socket closed or refused the connection."""


class AhpRpcError(RuntimeError):
    def __init__(self, method: str, code: int, message: str) -> None:
        super().__init__(f"{method} failed ({code}): {message}")
        self.method = method
        self.code = code


@dataclass(frozen=True)
class Notification:
    method: str
    params: dict[str, JsonValue]


type Params = AhpModel | dict[str, JsonValue]


def upgrade_uri(token: str) -> str:
    return f"ws://localhost/?tkn={quote(token, safe='')}"


def _wire(params: Params) -> dict[str, JsonValue]:
    return params.wire() if isinstance(params, AhpModel) else params


class Connection:
    def __init__(self, ws: ClientConnection, timeout: float = REQUEST_TIMEOUT_S) -> None:
        self._ws = ws
        self._timeout = timeout
        self._ids = itertools.count(1)
        self._waiting: dict[int, asyncio.Future[JsonValue]] = {}
        self._queue: asyncio.Queue[Notification | None] = asyncio.Queue()
        self._closed = asyncio.Event()
        self._reader = asyncio.create_task(self._read(), name="ahp-reader")

    @classmethod
    async def open(cls, endpoint: Endpoint, timeout: float = REQUEST_TIMEOUT_S) -> Connection:
        try:
            ws = await asyncio.wait_for(
                unix_connect(endpoint.socket_path, uri=upgrade_uri(endpoint.token), max_size=MAX_FRAME),
                timeout,
            )
        except (OSError, InvalidHandshake, TimeoutError) as exc:
            raise AhpConnectionError(f"cannot open the Agent Host socket: {type(exc).__name__}") from exc
        return cls(ws, timeout)

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    async def _read(self) -> None:
        try:
            async for frame in self._ws:
                text = frame if isinstance(frame, str) else frame.decode(errors="replace")
                await self._dispatch(text)
        except ConnectionClosed as exc:
            log.debug(f"agent host socket closed: {exc.rcvd}")
        finally:
            self._closed.set()
            for future in self._waiting.values():
                if not future.done():
                    future.set_exception(AhpConnectionError("the Agent Host socket closed"))
            self._waiting.clear()
            self._queue.put_nowait(None)

    async def _dispatch(self, text: str) -> None:
        try:
            message = json.loads(text)
        except json.JSONDecodeError:
            log.warning("agent host sent a frame that is not JSON")
            return
        if not isinstance(message, dict):
            return
        method, msg_id = message.get("method"), message.get("id")
        if isinstance(method, str):
            params = message.get("params")
            if msg_id is not None:
                await self._send(
                    {"jsonrpc": "2.0", "id": msg_id, "error": {"code": METHOD_NOT_FOUND, "message": method}}
                )
                return
            self._queue.put_nowait(Notification(method, params if isinstance(params, dict) else {}))
            return
        if not isinstance(msg_id, int):
            return
        future = self._waiting.pop(msg_id, None)
        if future is None or future.done():
            return
        error = message.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            future.set_exception(
                AhpRpcError("request", code if isinstance(code, int) else 0, str(error.get("message", "")))
            )
        else:
            future.set_result(message.get("result"))

    async def _send(self, message: dict[str, JsonValue]) -> None:
        try:
            await self._ws.send(json.dumps(message))
        except ConnectionClosed as exc:
            raise AhpConnectionError("the Agent Host socket closed") from exc

    async def request(self, method: str, params: Params) -> JsonValue:
        if self.closed:
            raise AhpConnectionError("the Agent Host socket is closed")
        msg_id = next(self._ids)
        future: asyncio.Future[JsonValue] = asyncio.get_running_loop().create_future()
        self._waiting[msg_id] = future
        try:
            await self._send({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": _wire(params)})
            return await asyncio.wait_for(future, self._timeout)
        except AhpRpcError as exc:
            raise AhpRpcError(method, exc.code, str(exc).partition(": ")[2]) from exc
        except TimeoutError as exc:
            raise AhpConnectionError(f"{method} timed out") from exc
        finally:
            self._waiting.pop(msg_id, None)

    async def notify(self, method: str, params: Params) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": _wire(params)})

    async def notifications(self) -> AsyncIterator[Notification]:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self._ws.close()
        self._reader.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._reader
