"""The owner's Webex identity, borrowed from the `webex-cli` tool.

`webex-cli` keeps an OAuth integration's token in a store next to itself and refreshes it. The driver
imports that tool only to ask it for a current access token (`get_access_token`), then talks to the
Webex REST API itself, so it can read message ids, delete cards and submit card actions, which the CLI
cannot. The token stays inside this module: it is never printed, logged, put in an exception or sent to
any host but `webexapis.com`.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from http.client import HTTPMessage
from importlib import machinery, util
from pathlib import Path
from types import ModuleType
from typing import IO, Any
from uuid import uuid4

from pydantic import JsonValue

from scripts.e2e.model import Download, DriverError, Msg, Person, Redactor, SubmitRefused

API = "https://webexapis.com/v1"
DEFAULT_CLI = Path.home() / ".local" / "bin" / "webex-cli"
BOT_SUFFIX = "@webex.bot"
LOCKED_RETRIES = 6
DETAIL_CHARS = 200
_FILENAME = re.compile(r"""filename\*?=(?:UTF-8'')?"?([^";]+)"?""", re.I)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: the Authorization header must not leave webexapis.com."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def load_cli(path: Path) -> ModuleType:
    """Import the webex-cli script (it has no `.py` suffix) without running its `main`."""
    if not path.is_file():
        raise DriverError(f"webex-cli not found at {path}; pass --webex-cli")
    loader = machinery.SourceFileLoader("webex_cli", str(path))
    spec = util.spec_from_loader("webex_cli", loader)
    if spec is None:
        raise DriverError(f"cannot load {path}")
    module = util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _multipart(fields: dict[str, str], file: Path) -> tuple[bytes, str]:
    boundary = f"----relaye2e{uuid4().hex}"
    parts: list[bytes] = []
    for key, value in fields.items():
        parts += [
            f"--{boundary}".encode(),
            f'Content-Disposition: form-data; name="{key}"'.encode(),
            b"",
            value.encode(),
        ]
    parts += [
        f"--{boundary}".encode(),
        f'Content-Disposition: form-data; name="files"; filename="{file.name}"'.encode(),
        b"Content-Type: application/octet-stream",
        b"",
        file.read_bytes(),
        f"--{boundary}--".encode(),
        b"",
    ]
    return b"\r\n".join(parts), f"multipart/form-data; boundary={boundary}"


class WebexCliUser:
    """`WebexUser` over the Webex REST API with the webex-cli integration's token."""

    def __init__(
        self,
        redact: Redactor,
        cli_path: Path = DEFAULT_CLI,
        *,
        token_source: Callable[[], str] | None = None,
    ) -> None:
        self._redact = redact
        self._cli_path = cli_path
        self._token_source = token_source
        self._module: ModuleType | None = None
        self._me: Person | None = None
        self._submit_scope: bool | None = None

    def _token(self) -> str:
        try:
            if self._token_source is not None:
                token = self._token_source()
            else:
                if self._module is None:
                    self._module = load_cli(self._cli_path)
                token = str(self._module.get_access_token())
        except SystemExit as exc:  # the CLI exits with a message on a failed refresh
            raise DriverError(f"webex-cli could not provide a token: {self._redact(str(exc.code))}") from None
        self._redact.add(token)
        return token

    def _http(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None = None,
        content_type: str | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        if not url.startswith(f"{API}/"):
            raise DriverError("refusing to send the token to a host other than webexapis.com")
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self._token()}"}
        if content_type:
            headers["Content-Type"] = content_type
        for attempt in range(4):
            request = urllib.request.Request(url, data=body, method=method, headers=headers)
            try:
                with _OPENER.open(request, timeout=60) as response:
                    return response.status, dict(response.headers), response.read()
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < 3:
                    time.sleep(min(int(exc.headers.get("Retry-After", "2")), 30))
                    continue
                return exc.code, dict(exc.headers), exc.read()
            except urllib.error.URLError as exc:
                raise DriverError(f"network error: {self._redact(str(exc.reason))}") from None
        raise DriverError("Webex kept answering 429")

    def _api(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str | None] | None = None,
        payload: dict[str, JsonValue] | None = None,
        ok: tuple[int, ...] = (200, 201, 204),
    ) -> tuple[int, Any]:
        url = f"{API}{path}"
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        body = json.dumps(payload).encode() if payload is not None else None
        status, _headers, raw = self._http(
            method, url, body=body, content_type="application/json" if body else None
        )
        if status not in ok:
            self._fail(method, path, status, raw)
        return status, json.loads(raw) if raw else {}

    def _fail(self, method: str, path: str, status: int, raw: bytes) -> None:
        detail = self._redact(raw.decode("utf-8", "replace"))[:DETAIL_CHARS]
        raise DriverError(f"{method} {path} failed (HTTP {status}): {detail}")

    async def _call[T](self, fn: Callable[[], T]) -> T:
        return await asyncio.to_thread(fn)

    async def whoami(self) -> Person:
        if self._me is None:
            _, data = await self._call(lambda: self._api("GET", "/people/me"))
            emails = data.get("emails") or [""]
            self._me = Person(id=str(data.get("id", "")), email=str(emails[0]))
        return self._me

    async def find_room(self, title: str) -> str:
        _, data = await self._call(
            lambda: self._api(
                "GET", "/rooms", params={"type": "direct", "sortBy": "lastactivity", "max": "100"}
            )
        )
        found = [r for r in data.get("items", []) if r.get("title") == title]
        if len(found) != 1:
            raise DriverError(f"{len(found)} direct rooms are titled {title!r}; pass --room-id")
        return str(found[0]["id"])

    async def counterpart(self, room_id: str) -> Person:
        me = await self.whoami()
        _, data = await self._call(
            lambda: self._api("GET", "/memberships", params={"roomId": room_id, "max": "10"})
        )
        others = [m for m in data.get("items", []) if m.get("personId") != me.id]
        if len(others) != 1:
            raise DriverError(f"the room has {len(others)} other members, expected the bot only")
        email = str(others[0].get("personEmail", ""))
        if not email.lower().endswith(BOT_SUFFIX):
            raise DriverError("the other member of the room is not a bot; refusing to drive it")
        return Person(id=str(others[0].get("personId", "")), email=email)

    async def send(
        self, room_id: str, text: str, *, parent_id: str | None = None, file: Path | None = None
    ) -> Msg:
        def go() -> Msg:
            if file is None:
                payload: dict[str, JsonValue] = {"roomId": room_id, "text": text}
                if parent_id:
                    payload["parentId"] = parent_id
                _, data = self._api("POST", "/messages", payload=payload)
                return Msg.model_validate(data)
            fields = {"roomId": room_id}
            if text:
                fields["text"] = text
            if parent_id:
                fields["parentId"] = parent_id
            body, content_type = _multipart(fields, file)
            status, _h, raw = self._http("POST", f"{API}/messages", body=body, content_type=content_type)
            if status not in (200, 201):
                self._fail("POST", "/messages", status, raw)
            return Msg.model_validate(json.loads(raw))

        return await self._call(go)

    async def list_messages(
        self, room_id: str, *, parent_id: str | None = None, limit: int = 50
    ) -> list[Msg]:
        # A thread root that was posted a moment ago may not be listable yet: Webex answers 404 until it is.
        accepted = (200, 404) if parent_id else (200,)
        status, data = await self._call(
            lambda: self._api(
                "GET",
                "/messages",
                params={"roomId": room_id, "parentId": parent_id, "max": str(limit)},
                ok=accepted,
            )
        )
        if status == 404:
            return []
        return [Msg.model_validate(item) for item in data.get("items", [])]

    async def get(self, message_id: str) -> Msg | None:
        def go() -> Msg | None:
            status, data = self._api("GET", f"/messages/{message_id}", ok=(200, 404))
            return Msg.model_validate(data) if status == 200 else None

        return await self._call(go)

    async def delete(self, message_id: str) -> None:
        await self._call(lambda: self._api("DELETE", f"/messages/{message_id}", ok=(200, 204, 404)))

    async def download(self, ref: str) -> Download:
        url = ref if ref.startswith("http") else f"{API}/contents/{ref}"

        def go() -> Download:
            for _ in range(LOCKED_RETRIES):
                status, headers, raw = self._http("GET", url)
                if status in (202, 423):  # Webex is still scanning the file
                    time.sleep(min(int(headers.get("Retry-After", "2")), 10))
                    continue
                if status != 200:
                    self._fail("GET", "file", status, raw)
                found = _FILENAME.search(headers.get("Content-Disposition", ""))
                name = found.group(1).strip() if found else ""
                return Download(name, headers.get("Content-Type", ""), raw)
            raise DriverError("the file stayed locked")

        return await self._call(go)

    async def submit_card(self, message_id: str, inputs: dict[str, JsonValue]) -> None:
        payload: dict[str, JsonValue] = {"type": "submit", "messageId": message_id, "inputs": inputs}

        def go() -> None:
            status, _headers, raw = self._http(
                "POST",
                f"{API}/attachment/actions",
                body=json.dumps(payload).encode(),
                content_type="application/json",
            )
            if status in (401, 403):
                detail = self._redact(raw.decode("utf-8", "replace"))[:DETAIL_CHARS]
                raise SubmitRefused(status, detail)
            if status not in (200, 201):
                self._fail("POST", "/attachment/actions", status, raw)

        await self._call(go)

    async def can_submit_cards(self) -> bool:
        """Probe the scope once with a card id that does not exist: 401/403 means the token may not
        submit; a 400 or 404 about the missing message means it may."""
        if self._submit_scope is None:
            ghost = base64.b64encode(b"ciscospark://us/MESSAGE/00000000-0000-0000-0000-000000000000").decode()

            def go() -> bool:
                status, _h, _raw = self._http(
                    "POST",
                    f"{API}/attachment/actions",
                    body=json.dumps({"type": "submit", "messageId": ghost, "inputs": {}}).encode(),
                    content_type="application/json",
                )
                return status not in (401, 403)

            self._submit_scope = await self._call(go)
        return self._submit_scope
