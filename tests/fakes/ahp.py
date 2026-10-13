"""A fake VS Code Agent Host: JSON-RPC over a WebSocket on a Unix socket, replaying the recorded S1 trace."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from pydantic import JsonValue
from websockets.asyncio.server import Server, ServerConnection, unix_serve
from websockets.http11 import Request, Response

from agent_relay.adapters.ahp.endpoints import Endpoint

TRACE = Path(__file__).resolve().parents[1] / "fixtures" / "ahp" / "ok_turn.trace.json"


def load_trace() -> list[dict[str, JsonValue]]:
    entries = json.loads(TRACE.read_text())
    return [e["msg"] for e in entries]


TRACE_MESSAGES = load_trace()
TRACE_SESSION = "ahp-session:/f7f70a60-951f-47ae-8e51-1d1f09eeb712"
TRACE_CHAT = "ahp-chat://a8aaf650-cb18-477c-960e-d1424bde9dd9/YWhwLXNlc3Npb246L2Y3ZjcwYTYwLTk1MWYtNDdhZS04ZTUxLTFkMWYwOWVlYjcxMg"
TRACE_TURN = "e3d361fa-2943-475f-887a-262d360d3b61"


def _initialize_result() -> JsonValue:
    for message in TRACE_MESSAGES:
        result = message.get("result")
        if isinstance(result, dict) and "snapshots" in result:
            return result
    raise AssertionError("trace has no initialize result")


def _turn_replay() -> list[dict[str, JsonValue]]:
    """Server notifications after the client's chat/turnStarted, in order."""
    start = next(i for i, msg in enumerate(TRACE_MESSAGES) if msg.get("method") == "dispatchAction")
    return [msg for msg in TRACE_MESSAGES[start + 1 :] if msg.get("method") == "action"]


@dataclass
class FakeAgentHost:
    socket_path: str
    token: str = "fake-connection-token"
    protocol_version: str = "0.10.0"
    auto_reply: bool = True
    page_size: int | None = None
    endless_cursor: bool = False
    sessions: dict[str, dict[str, JsonValue]] = field(default_factory=dict)
    requests: list[tuple[str, dict[str, JsonValue]]] = field(default_factory=list)
    notified: list[tuple[str, dict[str, JsonValue]]] = field(default_factory=list)
    dispatched: list[dict[str, JsonValue]] = field(default_factory=list)
    rejected: int = 0
    clients: set[ServerConnection] = field(default_factory=set)
    _server: Server | None = None
    _tasks: set[asyncio.Task[None]] = field(default_factory=set)

    @property
    def endpoint(self) -> Endpoint:
        return Endpoint(self.socket_path, self.protocol_version, self.token)

    async def start(self) -> FakeAgentHost:
        self._server = await unix_serve(self._handle, self.socket_path, process_request=self._check)
        return self

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def drop_clients(self) -> None:
        for client in list(self.clients):
            await client.close()

    def _check(self, connection: ServerConnection, request: Request) -> Response | None:
        tokens = parse_qs(urlparse(request.path).query).get("tkn", [])
        if tokens != [self.token]:
            self.rejected += 1
            return connection.respond(HTTPStatus.UNAUTHORIZED, "bad token\n")
        return None

    def methods(self) -> list[str]:
        return [method for method, _ in self.requests]

    def actions(self, kind: str | None = None) -> list[dict[str, JsonValue]]:
        found: list[dict[str, JsonValue]] = []
        for params in self.dispatched:
            action = params.get("action")
            if isinstance(action, dict) and (kind is None or action.get("type") == kind):
                found.append(action)
        return found

    def add_session(self, session_id: str, workspace: str, status: int = 8, title: str = "desk") -> str:
        resource = f"ahp-session:/{session_id}"
        self.sessions[resource] = {
            "provider": "copilotcli",
            "title": title,
            "status": status,
            "resource": resource,
            "createdAt": "2026-10-03T11:00:00.000Z",
            "modifiedAt": "2026-10-03T11:00:00.000Z",
            "workingDirectories": [Path(workspace).resolve().as_uri()],
        }
        return resource

    async def push(self, channel: str, action: dict[str, JsonValue]) -> None:
        note = {"jsonrpc": "2.0", "method": "action", "params": {"channel": channel, "action": action}}
        for client in list(self.clients):
            with contextlib.suppress(Exception):
                await client.send(json.dumps(note))

    async def replay_turn(self, session: str, chat: str, turn_id: str) -> None:
        for message in _turn_replay():
            text = json.dumps(message)
            text = text.replace(TRACE_CHAT, chat).replace(TRACE_SESSION, session).replace(TRACE_TURN, turn_id)
            for client in list(self.clients):
                await client.send(text)
            await asyncio.sleep(0)

    async def _handle(self, connection: ServerConnection) -> None:
        self.clients.add(connection)
        try:
            async for frame in connection:
                message = json.loads(frame)
                method, params = message.get("method"), message.get("params") or {}
                if "id" not in message:
                    if method == "dispatchAction":
                        await self._on_dispatch(params)
                    else:
                        self.notified.append((method, params))
                    continue
                self.requests.append((method, params))
                reply: dict[str, JsonValue] = {"jsonrpc": "2.0", "id": message["id"]}
                try:
                    reply["result"] = self._answer(method, params)
                except KeyError as exc:
                    reply["error"] = {"code": -32601, "message": str(exc)}
                await connection.send(json.dumps(reply))
        except Exception:
            return
        finally:
            self.clients.discard(connection)

    def _answer(self, method: str, params: dict[str, JsonValue]) -> JsonValue:
        channel = params.get("channel")
        match method:
            case "initialize":
                return _initialize_result()
            case "listSessions":
                items: list[JsonValue] = list(self.sessions.values())
                if self.page_size is None:
                    return {"items": items}
                cursor = params.get("cursor")
                start = int(cursor.removeprefix("page-")) if isinstance(cursor, str) else 0
                end = start + self.page_size
                page: dict[str, JsonValue] = {"items": items[start:end]}
                if end < len(items) or self.endless_cursor:
                    page["nextCursor"] = f"page-{end}"
                return page
            case "createSession":
                assert isinstance(channel, str)
                dirs = params.get("workingDirectories")
                self.sessions[channel] = {
                    "provider": params.get("provider"),
                    "title": "",
                    "status": 1,
                    "resource": channel,
                    "createdAt": "2026-10-03T11:00:00.000Z",
                    "modifiedAt": "2026-10-03T11:00:00.000Z",
                    "workingDirectories": dirs if isinstance(dirs, list) else [],
                }
                return None
            case "subscribe":
                return {"snapshot": {"resource": channel, "state": {}, "fromSeq": 0}}
            case "createChat" | "disposeSession":
                return None
        raise KeyError(method)

    async def _on_dispatch(self, params: dict[str, JsonValue]) -> None:
        self.dispatched.append(params)
        action, chat = params.get("action"), params.get("channel")
        if not self.auto_reply or not isinstance(action, dict) or not isinstance(chat, str):
            return
        if action.get("type") != "chat/turnStarted":
            return
        turn_id = action.get("turnId")
        session = next((s for s in self.sessions if chat.endswith(_b64(s))), None)
        if isinstance(turn_id, str) and session is not None:
            task = asyncio.get_running_loop().create_task(self.replay_turn(session, chat, turn_id))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)


def _b64(session: str) -> str:
    return base64.urlsafe_b64encode(session.encode()).decode().rstrip("=")
