"""An in-memory Webex: REST (messages, edits, cards, rooms, memberships), U2C/WDM and limit checks."""

from __future__ import annotations

import base64
import itertools
import json
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs

import httpx
from pydantic import JsonValue

API = "https://webexapis.com/v1"
U2C = "https://u2c.fake/u2c/api/v1/catalog"
WDM = "https://wdm.fake/wdm/api/v1"
CONV = "https://conv.fake/conversation/api/v1"
MAX_TEXT_BYTES = 7_439
MAX_EDITS = 10
MAX_CARD_MESSAGE_BYTES = 22_740


@dataclass
class FakeMessage:
    id: str
    room_id: str
    person_id: str
    person_email: str
    text: str = ""
    markdown: str = ""
    parent_id: str | None = None
    attachments: list[JsonValue] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    file_bytes: bytes = b""
    created: str = ""
    deleted: bool = False

    def as_json(self, room_type: str) -> dict[str, JsonValue]:
        data: dict[str, JsonValue] = {
            "id": self.id,
            "roomId": self.room_id,
            "roomType": room_type,
            "personId": self.person_id,
            "personEmail": self.person_email,
            "text": self.text,
            "markdown": self.markdown,
            "created": self.created,
        }
        if self.parent_id:
            data["parentId"] = self.parent_id
        if self.files:
            data["files"] = list(self.files)
        return data


def _multipart_fields(request: httpx.Request) -> tuple[dict[str, str], dict[str, bytes]]:
    match = re.search(r"boundary=([^;]+)", request.headers["content-type"])
    assert match
    boundary = match.group(1).encode()
    fields: dict[str, str] = {}
    files: dict[str, bytes] = {}
    for part in request.content.split(b"--" + boundary):
        if b"\r\n\r\n" not in part:
            continue
        head, body = part.split(b"\r\n\r\n", 1)
        body = body.removesuffix(b"\r\n")
        name = re.search(rb'name="([^"]+)"', head)
        filename = re.search(rb'filename="([^"]+)"', head)
        if not name:
            continue
        if filename:
            files[filename.group(1).decode()] = body
        else:
            fields[name.group(1).decode()] = body.decode()
    return fields, files


class FakeWebex:
    def __init__(self, user_email: str = "me@example.com") -> None:
        self.bot_id = "bot-person"
        self.user_id = "user-person"
        self.user_email = user_email
        self.direct_room = "room-direct"
        self.rooms: dict[str, str] = {self.direct_room: "direct", "room-group": "group"}
        self.messages: dict[str, FakeMessage] = {}
        self.order: list[str] = []
        self.edits: dict[str, int] = {}
        self.actions: dict[str, dict[str, JsonValue]] = {}
        self.devices: dict[str, dict[str, JsonValue]] = {}
        self.left_rooms: list[str] = []
        self.violations: list[str] = []
        self.rate_limit_next = 0
        self.rate_limited = 0
        self.requests: list[str] = []
        self._ids = itertools.count(1)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.transport(), headers={"Authorization": "Bearer test-token"})

    def _next(self, prefix: str) -> str:
        return f"{prefix}-{next(self._ids)}"

    # scenario helpers

    def user_says(
        self,
        text: str,
        parent_id: str | None = None,
        room: str | None = None,
        person: str | None = None,
        email: str | None = None,
    ) -> str:
        msg = FakeMessage(
            id=self._next("msg"),
            room_id=room or self.direct_room,
            person_id=person or self.user_id,
            person_email=email or self.user_email,
            text=text,
            parent_id=parent_id,
            created=f"t{len(self.order):06d}",
        )
        self.messages[msg.id] = msg
        self.order.append(msg.id)
        return msg.id

    def user_clicks(
        self,
        message_id: str,
        inputs: dict[str, JsonValue],
        person: str | None = None,
        room: str | None = None,
    ) -> str:
        action_id = self._next("act")
        self.actions[action_id] = {
            "id": action_id,
            "type": "submit",
            "messageId": message_id,
            "personId": person or self.user_id,
            "roomId": room or self.direct_room,
            "inputs": inputs,
        }
        return action_id

    def bot_messages(self) -> list[FakeMessage]:
        return [self.messages[i] for i in self.order if self.messages[i].person_id == self.bot_id]

    def last_card(self) -> tuple[FakeMessage, dict[str, JsonValue]]:
        for msg in reversed(self.bot_messages()):
            if msg.attachments:
                content = msg.attachments[0]
                assert isinstance(content, dict) and isinstance(content["content"], dict)
                return msg, content["content"]
        raise AssertionError("no card posted")

    # transport

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(f"{request.method} {url.split('?')[0]}")
        if url.startswith(API) and self.rate_limit_next > 0:
            self.rate_limit_next -= 1
            self.rate_limited += 1
            return httpx.Response(429, headers={"Retry-After": "3"}, json={"message": "slow down"})
        if url.startswith(U2C):
            return httpx.Response(200, json={"serviceLinks": {"wdm": WDM}})
        if url.startswith(WDM):
            return self._wdm(request)
        if url.startswith(CONV):
            return self._conversation(request)
        path = request.url.path.removeprefix("/v1")
        return self._api(request, path)

    def _wdm(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/devices"):
            return httpx.Response(200, json={"devices": list(self.devices.values())})
        if request.method == "POST" and path.endswith("/devices"):
            device_id = self._next("dev")
            body = json.loads(request.content)
            device: dict[str, JsonValue] = {
                **body,
                "url": f"{WDM}/devices/{device_id}",
                "webSocketUrl": f"wss://mercury.fake/{device_id}",
            }
            self.devices[device_id] = device
            return httpx.Response(200, json=device)
        if request.method == "DELETE":
            self.devices.pop(path.rsplit("/", 1)[-1], None)
            return httpx.Response(204)
        return httpx.Response(404)

    def _conversation(self, request: httpx.Request) -> httpx.Response:
        kind, _, raw_id = request.url.path.rpartition("/")
        prefix = "act" if kind.endswith("attachment/actions") else "msg"
        uuid = raw_id
        return httpx.Response(200, json={"id": f"{prefix}-{base64.b64decode(uuid).decode()}"})

    def activity_for(self, rest_id: str, verb: str = "post") -> dict[str, JsonValue]:
        """A websocket frame for a stored message or action (the id round-trips through base64)."""
        raw = rest_id.split("-", 1)[1]
        encoded = base64.b64encode(raw.encode()).decode()
        return {
            "data": {
                "eventType": "conversation.activity",
                "activity": {
                    "id": encoded,
                    "verb": verb,
                    "target": {"id": "conv-1", "url": f"{CONV}/conversations/conv-1"},
                },
            }
        }

    def _api(self, request: httpx.Request, path: str) -> httpx.Response:
        method = request.method
        if method == "GET" and path == "/people/me":
            return httpx.Response(
                200, json={"id": self.bot_id, "emails": ["relay@webex.bot"], "displayName": "relay"}
            )
        if path == "/messages" and method == "POST":
            return self._post_message(request)
        if path == "/messages" and method == "GET":
            return self._list(request)
        if path.startswith("/messages/"):
            return self._message(request, path.rsplit("/", 1)[-1])
        if path.startswith("/attachment/actions/"):
            action = self.actions.get(path.rsplit("/", 1)[-1])
            return httpx.Response(200, json=action) if action else httpx.Response(404)
        if path.startswith("/rooms/"):
            room_id = path.rsplit("/", 1)[-1]
            return httpx.Response(200, json={"id": room_id, "type": self.rooms.get(room_id, "group")})
        if path == "/memberships" and method == "GET":
            params = parse_qs(request.url.query.decode())
            room = params["roomId"][0]
            return httpx.Response(200, json={"items": [{"id": f"mem:{room}"}]})
        if path.startswith("/memberships/") and method == "DELETE":
            self.left_rooms.append(path.rsplit(":", 1)[-1])
            return httpx.Response(204)
        return httpx.Response(404, json={"message": f"no route {method} {path}"})

    def _post_message(self, request: httpx.Request) -> httpx.Response:
        if request.headers["content-type"].startswith("multipart/"):
            fields, files = _multipart_fields(request)
            body: dict[str, JsonValue] = dict(fields)
        else:
            body = json.loads(request.content)
            files = {}
            if request.content != json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode():
                self.violations.append("request body is not minified")
        markdown = str(body.get("markdown", ""))
        if len(markdown.encode()) > MAX_TEXT_BYTES:
            self.violations.append(f"message of {len(markdown.encode())} bytes")
            return httpx.Response(400, json={"message": "too long"})
        attachments = body.get("attachments")
        if attachments and files:
            self.violations.append("attachments and files in one message")
        if isinstance(attachments, list) and attachments and isinstance(attachments[0], dict):
            card_json = json.dumps(attachments[0]["content"], separators=(",", ":"), ensure_ascii=False)
            if len(markdown.encode()) + len(card_json.encode()) > MAX_CARD_MESSAGE_BYTES:
                self.violations.append("text and card over 22,740 bytes")
                return httpx.Response(400, json={"message": "too long"})
        parent = body.get("parentId")
        room = body.get("roomId")
        if not isinstance(room, str):
            assert body.get("toPersonEmail") == self.user_email
            room = self.direct_room
        msg = FakeMessage(
            id=self._next("msg"),
            room_id=room,
            person_id=self.bot_id,
            person_email="relay@webex.bot",
            markdown=markdown,
            text=markdown,
            parent_id=parent if isinstance(parent, str) else None,
            attachments=list(attachments) if isinstance(attachments, list) else [],
            files=list(files),
            file_bytes=next(iter(files.values()), b""),
            created=f"t{len(self.order):06d}",
        )
        self.messages[msg.id] = msg
        self.order.append(msg.id)
        return httpx.Response(200, json=msg.as_json(self.rooms.get(room, "direct")))

    def _list(self, request: httpx.Request) -> httpx.Response:
        params = parse_qs(request.url.query.decode())
        room = params["roomId"][0]
        limit = int(params.get("max", ["50"])[0])
        before = params.get("beforeMessage", [None])[0]
        ids = [
            i
            for i in reversed(self.order)
            if self.messages[i].room_id == room and not self.messages[i].deleted
        ]
        if before is not None and before in ids:
            ids = ids[ids.index(before) + 1 :]
        items = [self.messages[i].as_json(self.rooms.get(room, "direct")) for i in ids[:limit]]
        return httpx.Response(200, json={"items": items})

    def _message(self, request: httpx.Request, message_id: str) -> httpx.Response:
        msg = self.messages.get(message_id)
        if msg is None:
            return httpx.Response(404)
        if request.method == "GET":
            return httpx.Response(200, json=msg.as_json(self.rooms.get(msg.room_id, "direct")))
        if request.method == "PUT":
            self.edits[message_id] = self.edits.get(message_id, 0) + 1
            if self.edits[message_id] > MAX_EDITS:
                self.violations.append(f"message {message_id} edited {self.edits[message_id]} times")
                return httpx.Response(400)
            body = json.loads(request.content)
            msg.markdown = str(body["markdown"])
            if len(msg.markdown.encode()) > MAX_TEXT_BYTES:
                self.violations.append("edit too long")
            return httpx.Response(200, json=msg.as_json("direct"))
        if request.method == "DELETE":
            msg.deleted = True
            return httpx.Response(204)
        return httpx.Response(405)
