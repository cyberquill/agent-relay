"""Wire models, errors and the two I/O protocols (`WebexUser`, `Clock`) the driver is written against."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from agent_relay.scrub import scrub_text

REDACTED = "<redacted>"
MIN_SECRET_CHARS = 8


class DriverError(Exception):
    """A failure whose message is safe to show: it never carries a token."""


class SubmitRefused(DriverError):
    """Webex refused a card submit for this token (no scope, or not allowed)."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"card submit refused (HTTP {status}): {detail}")
        self.status = status


class ScenarioFailed(Exception):
    """An assertion about the relay's behaviour did not hold."""


class ScenarioSkipped(Exception):
    """A scenario cannot run here (labelled in the report; never a pytest skip)."""


class BudgetExceeded(DriverError):
    """Creating one more session would pass the per-adapter cap."""


class Redactor:
    """Removes every secret the driver has seen, then anything the relay's own scrubber recognises."""

    def __init__(self) -> None:
        self._secrets: set[str] = set()

    def add(self, secret: str) -> None:
        if len(secret) >= MIN_SECRET_CHARS:
            self._secrets.add(secret)

    def __call__(self, text: str) -> str:
        out = text
        for secret in sorted(self._secrets, key=len, reverse=True):
            out = out.replace(secret, REDACTED)
        return scrub_text(out)


class Msg(BaseModel):
    """One Webex message, as the owner's token sees it."""

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    id: str
    room_id: str = Field(default="", alias="roomId")
    person_email: str = Field(default="", alias="personEmail")
    text: str = ""
    markdown: str = ""
    parent_id: str | None = Field(default=None, alias="parentId")
    files: list[str] = Field(default_factory=list)
    attachments: list[dict[str, JsonValue]] = Field(default_factory=list)
    created: str = ""

    @property
    def body(self) -> str:
        """Plain and markdown text together, for matching."""
        return f"{self.text}\n{self.markdown}" if self.markdown else self.text


class Person(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    id: str
    email: str


@dataclass(frozen=True)
class Download:
    name: str
    content_type: str
    data: bytes


class WebexUser(Protocol):
    """What the driver may do as the owner. The real one is `webex_cli_user.WebexCliUser`."""

    async def whoami(self) -> Person: ...

    async def find_room(self, title: str) -> str: ...

    async def counterpart(self, room_id: str) -> Person: ...

    async def send(
        self, room_id: str, text: str, *, parent_id: str | None = None, file: Path | None = None
    ) -> Msg: ...

    async def list_messages(
        self, room_id: str, *, parent_id: str | None = None, limit: int = 50
    ) -> list[Msg]: ...

    async def get(self, message_id: str) -> Msg | None: ...

    async def delete(self, message_id: str) -> None: ...

    async def download(self, ref: str) -> Download: ...

    async def submit_card(self, message_id: str, inputs: dict[str, JsonValue]) -> None: ...

    async def can_submit_cards(self) -> bool: ...


class Clock(Protocol):
    def now(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class RealClock:
    def now(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
