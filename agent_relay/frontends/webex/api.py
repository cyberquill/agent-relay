"""Async Webex REST client with the platform limits enforced in code.

Limits: 7,439 bytes of message text, 10 edits per message, and `Retry-After` honoured on 429.
Everything posted is scrubbed for secrets first.
"""

from __future__ import annotations

import asyncio
import mimetypes
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from email.message import Message as MimeMessage
from typing import cast
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from agent_relay.core.inbox import CHUNK, Incoming, SkipFileError, TooLargeError, human_size
from agent_relay.frontends.webex.cards.common import fit_card, minify
from agent_relay.log import get_logger
from agent_relay.scrub import scrub_obj, scrub_text

log = get_logger(__name__)

MAX_TEXT_BYTES = 7_439
MAX_EDITS = 10
MAX_RETRIES = 5
LOCKED_RETRIES = 5
LOCKED_WAIT_MAX_S = 30.0
HTTP_LOCKED = 423
HTTP_REDIRECT = 300
HTTP_ERROR = 400
CARD_CONTENT_TYPE = "application/vnd.microsoft.card.adaptive"
CARD_DROPPED = "_The interactive card was too large to send._"


class WebexError(RuntimeError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"Webex API {status}: {detail}")
        self.status = status


class MessageTooLargeError(ValueError):
    pass


class EditLimitError(RuntimeError):
    pass


class Message(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str
    room_id: str = Field(default="", alias="roomId")
    room_type: str = Field(default="", alias="roomType")
    parent_id: str | None = Field(default=None, alias="parentId")
    person_id: str = Field(default="", alias="personId")
    person_email: str = Field(default="", alias="personEmail")
    text: str = ""
    markdown: str = ""
    files: list[str] = Field(default_factory=list)
    created: str = ""


class AttachmentAction(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str
    type: str = "submit"
    message_id: str = Field(default="", alias="messageId")
    person_id: str = Field(default="", alias="personId")
    room_id: str = Field(default="", alias="roomId")
    inputs: dict[str, JsonValue] = Field(default_factory=dict)


class Room(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    type: str = "direct"
    title: str = ""


class Person(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    emails: list[str] = Field(default_factory=list)
    display_name: str = Field(default="", alias="displayName")


def text_bytes(text: str) -> int:
    return len(text.encode("utf-8"))


def scrub_attachment(content: bytes) -> bytes:
    """Attachments are text (diffs, long answers), scrubbed like messages. Binary is refused."""
    try:
        decoded = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("only UTF-8 text attachments are sent, so they can be scrubbed") from exc
    return scrub_text(decoded).encode("utf-8")


def _download_name(disposition: str | None, mime: str | None) -> str:
    """The file name Webex sends in Content-Disposition; without one, `file` plus the type's extension."""
    if disposition:
        header = MimeMessage()
        header["Content-Disposition"] = disposition
        found = header.get_filename()
        if found:
            return found
    extension = mimetypes.guess_extension((mime or "").split(";", 1)[0].strip()) or ""
    return f"file{extension}"


class WebexApi:
    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str = "https://webexapis.com/v1",
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._client = client
        self._base = base_url.rstrip("/")
        self._sleep = sleep
        self._edits: dict[str, int] = {}

    async def _request(
        self,
        method: str,
        endpoint: str,
        *,
        json_body: dict[str, JsonValue] | None = None,
        params: dict[str, str | int] | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
        data: dict[str, str] | None = None,
    ) -> httpx.Response:
        url = endpoint if endpoint.startswith("http") else f"{self._base}{endpoint}"
        # Compact JSON, so the size the card check counts is the size that is sent.
        content = minify(json_body).encode() if json_body is not None else None
        headers = {"Content-Type": "application/json"} if content is not None else None
        response = await self._client.request(
            method, url, content=content, headers=headers, params=params, files=files, data=data
        )
        for _ in range(MAX_RETRIES):
            if response.status_code != 429:
                break
            wait = float(response.headers.get("Retry-After", "1"))
            log.warning(f"Webex 429 on {method} {endpoint}; retrying in {wait:.0f}s")
            await self._sleep(wait)
            response = await self._client.request(
                method, url, content=content, headers=headers, params=params, files=files, data=data
            )
        if response.status_code >= 400:
            raise WebexError(response.status_code, scrub_text(response.text[:300]))
        return response

    async def _json(
        self,
        method: str,
        endpoint: str,
        *,
        json_body: dict[str, JsonValue] | None = None,
        params: dict[str, str | int] | None = None,
    ) -> dict[str, JsonValue]:
        response = await self._request(method, endpoint, json_body=json_body, params=params)
        if not response.content:
            return {}
        return cast(dict[str, JsonValue], response.json())

    # identity and rooms

    async def me(self) -> Person:
        return Person.model_validate(await self._json("GET", "/people/me"))

    async def get_room(self, room_id: str) -> Room:
        return Room.model_validate(await self._json("GET", f"/rooms/{room_id}"))

    async def leave_room(self, room_id: str, my_person_id: str) -> None:
        found = await self._json("GET", "/memberships", params={"roomId": room_id, "personId": my_person_id})
        items = found.get("items")
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and isinstance(item.get("id"), str):
                    await self._request("DELETE", f"/memberships/{item['id']}")

    # messages

    async def get_message(self, message_id: str) -> Message:
        return Message.model_validate(await self._json("GET", f"/messages/{message_id}"))

    async def list_messages(
        self, room_id: str, max_items: int = 50, before: str | None = None
    ) -> list[Message]:
        params: dict[str, str | int] = {"roomId": room_id, "max": max_items}
        if before:
            params["beforeMessage"] = before
        found = await self._json("GET", "/messages", params=params)
        items = found.get("items")
        return [Message.model_validate(i) for i in items] if isinstance(items, list) else []

    async def get_attachment_action(self, action_id: str) -> AttachmentAction:
        return AttachmentAction.model_validate(await self._json("GET", f"/attachment/actions/{action_id}"))

    async def create_message(
        self,
        markdown: str,
        *,
        room_id: str | None = None,
        to_person_email: str | None = None,
        parent_id: str | None = None,
        card: dict[str, JsonValue] | None = None,
        file: tuple[str, bytes, str] | None = None,
    ) -> Message:
        text = scrub_text(markdown)
        attachment: dict[str, JsonValue] | None = None
        if card is not None:
            cleaned_card = cast(dict[str, JsonValue], scrub_obj(card))
            attachment = fit_card(text, cleaned_card)
            if attachment is None:
                log.warning("card too large for one Webex message; sending its text only")
                text = f"{text}\n\n{CARD_DROPPED}"
        if text_bytes(text) > MAX_TEXT_BYTES:
            raise MessageTooLargeError(f"{text_bytes(text)} bytes > {MAX_TEXT_BYTES}")
        target: dict[str, str] = {}
        if room_id:
            target["roomId"] = room_id
        elif to_person_email:
            target["toPersonEmail"] = to_person_email
        else:
            raise ValueError("room_id or to_person_email is required")
        if parent_id:
            target["parentId"] = parent_id
        if file is not None:
            name, content, mime = file
            cleaned = scrub_attachment(content)
            response = await self._request(
                "POST", "/messages", data={**target, "markdown": text}, files={"files": (name, cleaned, mime)}
            )
            return Message.model_validate(response.json())
        body: dict[str, JsonValue] = {**target, "markdown": text}
        if attachment is not None:
            body["attachments"] = [{"contentType": CARD_CONTENT_TYPE, "content": attachment}]
        return Message.model_validate(await self._json("POST", "/messages", json_body=body))

    def edits_used(self, message_id: str) -> int:
        return self._edits.get(message_id, 0)

    async def edit_message(self, message_id: str, room_id: str, markdown: str) -> Message:
        used = self._edits.get(message_id, 0)
        if used >= MAX_EDITS:
            raise EditLimitError(f"message {message_id} already edited {used} times")
        text = scrub_text(markdown)
        if text_bytes(text) > MAX_TEXT_BYTES:
            raise MessageTooLargeError(f"{text_bytes(text)} bytes > {MAX_TEXT_BYTES}")
        result = await self._json(
            "PUT", f"/messages/{message_id}", json_body={"roomId": room_id, "markdown": text}
        )
        self._edits[message_id] = used + 1
        return Message.model_validate(result)

    async def delete_message(self, message_id: str) -> None:
        await self._request("DELETE", f"/messages/{message_id}")

    # files sent to the bot

    def _is_webex_url(self, url: str) -> bool:
        wanted, found = urlsplit(self._base), urlsplit(url)
        return (found.scheme, found.netloc.lower()) == (wanted.scheme, wanted.netloc.lower())

    async def _open_download(self, url: str) -> httpx.Response:
        """The streamed response, once Webex has finished scanning the file (423, bounded)."""
        for attempt in range(LOCKED_RETRIES + 1):
            response = await self._client.send(
                self._client.build_request("GET", url), stream=True, follow_redirects=False
            )
            if response.status_code not in (HTTP_LOCKED, 429) or attempt == LOCKED_RETRIES:
                return response
            try:
                wait = min(float(response.headers.get("Retry-After", "2")), LOCKED_WAIT_MAX_S)
            except ValueError:
                wait = 2.0
            await response.aclose()
            log.info(f"Webex is still scanning a file; retrying in {wait:.0f}s")
            await self._sleep(wait)
        raise AssertionError("unreachable")

    @asynccontextmanager
    async def download(self, url: str, max_bytes: int) -> AsyncIterator[Incoming]:
        """Stream a message's file. The bot token goes only to the Webex API host; nothing is followed."""
        if not self._is_webex_url(url):
            raise SkipFileError("file", "not a Webex file link")
        response = await self._open_download(url)
        try:
            mime = response.headers.get("Content-Type")
            name = _download_name(response.headers.get("Content-Disposition"), mime)
            if response.status_code == HTTP_LOCKED:
                raise SkipFileError(name, "Webex is still scanning it; send it again in a minute")
            if HTTP_REDIRECT <= response.status_code < HTTP_ERROR:
                raise SkipFileError(name, "Webex redirected the download")
            if response.status_code >= HTTP_ERROR:
                raise SkipFileError(name, f"Webex refused the download (HTTP {response.status_code})")
            declared = response.headers.get("Content-Length", "")
            if declared.isdigit() and int(declared) > max_bytes:
                raise TooLargeError(name, f"over {human_size(max_bytes)}")
            yield Incoming(name, mime, response.aiter_bytes(CHUNK))
        finally:
            await response.aclose()
