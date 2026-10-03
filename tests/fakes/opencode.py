"""A fake `opencode serve`: the recorded `/doc`, sessions, prompt/abort, permission and question replies,
and `/global/event` SSE fan-out. Served through an httpx MockTransport."""

from __future__ import annotations

import asyncio
import base64
import copy
import json
from collections.abc import AsyncIterator, Mapping
from pathlib import Path

import httpx
from pydantic import JsonValue

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "opencode"
DOC: dict[str, JsonValue] = json.loads((FIXTURES / "doc.json").read_text())
OK_SSE = FIXTURES / "ok_session.sse"

PROVIDERS: dict[str, JsonValue] = {
    "providers": [
        {
            "id": "github-copilot",
            "name": "GitHub Copilot",
            "models": {
                "gpt-5-mini": {"id": "gpt-5-mini", "name": "GPT-5 mini"},
                "claude-x": {"name": "Claude X"},
            },
        },
        {"id": "anthropic", "name": "Anthropic", "models": {"sonnet": {"name": "Sonnet"}}},
    ],
    "default": {"github-copilot": "gpt-5-mini"},
}


def legacy_doc() -> dict[str, JsonValue]:
    doc = copy.deepcopy(DOC)
    routes = doc["paths"]
    assert isinstance(routes, dict)
    routes.pop("/permission/{requestID}/reply")
    return doc


class FakeOpenCode:
    def __init__(
        self, base_url: str = "http://127.0.0.1:4096", password: str | None = None, legacy: bool = False
    ) -> None:
        self.base_url = base_url
        self.password = password
        self.doc = legacy_doc() if legacy else DOC
        self.sessions: dict[str, str | None] = {}
        self.prompts: list[tuple[str, dict[str, JsonValue], str | None]] = []
        self.aborted: list[str] = []
        self.replies: list[tuple[str, JsonValue]] = []
        self.unauthorized = 0
        self.up = True
        self.auto_reply: str | None = None
        self._subscribers: list[asyncio.Queue[bytes | None]] = []
        self._ids = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    @property
    def subscribers(self) -> int:
        return len(self._subscribers)

    # scripting

    def emit(self, kind: str, properties: Mapping[str, object], directory: str | None = "/w/proj") -> None:
        envelope: dict[str, object] = {
            "payload": {"id": f"evt_{kind}", "type": kind, "properties": properties}
        }
        if directory is not None:
            envelope["directory"] = directory
        self._push(f"data: {json.dumps(envelope)}\n\n".encode())

    def replay(self, file: Path = OK_SSE, session_id: str | None = None) -> None:
        text = file.read_text()
        if session_id:
            text = text.replace("ses_efe95c69dffeDbf32LYaGpckLV", session_id)
        for block in text.split("\n\n"):
            if block.strip():
                self._push(f"{block}\n\n".encode())

    def close_streams(self) -> None:
        for queue in self._subscribers:
            queue.put_nowait(None)

    def _push(self, chunk: bytes) -> None:
        for queue in self._subscribers:
            queue.put_nowait(chunk)

    def ok_turn(self, session_id: str, text: str) -> None:
        mid, pid = f"msg_{session_id}_a", f"prt_{session_id}_t"
        self.emit("session.status", {"sessionID": session_id, "status": {"type": "busy"}})
        self.emit("message.updated", {"sessionID": session_id, "info": {"id": mid, "role": "assistant"}})
        part = {"id": pid, "sessionID": session_id, "messageID": mid, "type": "text", "text": ""}
        self.emit("message.part.updated", {"sessionID": session_id, "part": part})
        delta = {"sessionID": session_id, "messageID": mid, "partID": pid, "field": "text", "delta": text}
        self.emit("message.part.delta", delta)
        self.emit("message.part.updated", {"sessionID": session_id, "part": {**part, "text": text}})
        self.emit("session.idle", {"sessionID": session_id})

    # HTTP

    def _authorized(self, request: httpx.Request) -> bool:
        if self.password is None:
            return True
        expected = base64.b64encode(f"opencode:{self.password}".encode()).decode()
        return bool(request.headers.get("authorization") == f"Basic {expected}")

    async def _stream(self, queue: asyncio.Queue[bytes | None]) -> AsyncIterator[bytes]:
        try:
            yield b'data: {"payload": {"id": "evt_0", "type": "server.connected", "properties": {}}}\n\n'
            while True:
                chunk = await queue.get()
                if chunk is None:
                    return
                yield chunk
        finally:
            self._subscribers.remove(queue)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        if not self.up:
            raise httpx.ConnectError("connection refused", request=request)
        if not self._authorized(request):
            self.unauthorized += 1
            return httpx.Response(401, text="Unauthorized")
        route = request.url.path
        directory = request.url.params.get("directory")
        body: JsonValue = json.loads(request.content) if request.content else None
        parts = route.strip("/").split("/")
        match request.method, parts:
            case "GET", ["doc"]:
                return httpx.Response(200, json=self.doc)
            case "GET", ["config", "providers"]:
                return httpx.Response(200, json=PROVIDERS)
            case "GET", ["global", "event"]:
                queue: asyncio.Queue[bytes | None] = asyncio.Queue()
                self._subscribers.append(queue)
                return httpx.Response(
                    200, headers={"content-type": "text/event-stream"}, content=self._stream(queue)
                )
            case "POST", ["session"]:
                self._ids += 1
                sid = f"ses_fake{self._ids}"
                self.sessions[sid] = directory
                title = body.get("title") if isinstance(body, dict) else None
                return httpx.Response(200, json={"id": sid, "title": title, "directory": directory})
            case "POST", ["session", sid, "prompt_async"]:
                assert isinstance(body, dict)
                self.prompts.append((sid, body, directory))
                if self.auto_reply is not None:
                    self.ok_turn(sid, self.auto_reply)
                return httpx.Response(204)
            case "POST", ["session", sid, "abort"]:
                self.aborted.append(sid)
                return httpx.Response(200, json=True)
            case "POST", ["permission", request_id, "reply"]:
                self.replies.append((f"permission:{request_id}", body))
                return httpx.Response(200, json=True)
            case "POST", ["session", sid, "permissions", request_id]:
                self.replies.append((f"legacy:{sid}:{request_id}", body))
                return httpx.Response(200, json=True)
            case "POST", ["question", request_id, "reply"]:
                self.replies.append((f"question:{request_id}", body))
                return httpx.Response(200, json=True)
            case "POST", ["question", request_id, "reject"]:
                self.replies.append((f"reject:{request_id}", body))
                return httpx.Response(200, json=True)
        return httpx.Response(404, text=f"no route {request.method} {route}")


class FakeOpenCodeFleet:
    """Several fake servers behind one transport, keyed by base URL."""

    def __init__(self, *servers: FakeOpenCode) -> None:
        self.servers = {s.base_url: s for s in servers}

    def transport(self) -> httpx.MockTransport:
        async def route(request: httpx.Request) -> httpx.Response:
            base = f"{request.url.scheme}://{request.url.host}:{request.url.port}"
            server = self.servers.get(base)
            if server is None:
                raise httpx.ConnectError("connection refused", request=request)
            return await server.handle(request)

        return httpx.MockTransport(route)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.transport())
