"""Session and pending-request records held in the registry."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from agent_relay.models.events import Harness, Origin, utcnow


class SessionStatus(StrEnum):
    ACTIVE = "active"
    RUNNING = "running"
    IDLE = "idle"
    PARKED = "parked"
    ENDED = "ended"


class SessionRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_key: str
    harness: Harness
    harness_id: str
    workspace: str | None = None
    model: str | None = None
    origin: Origin = Origin.DESK
    status: SessionStatus = SessionStatus.ACTIVE
    webex_thread_id: str | None = None
    # The Webex bot whose space holds the thread; None on a thread made before there were several.
    webex_bot: str | None = None
    title: str | None = None
    extra: dict[str, JsonValue] = Field(default_factory=dict)
    created: datetime = Field(default_factory=utcnow)
    updated: datetime = Field(default_factory=utcnow)


PendingKind = Literal["approval", "question"]
PendingStatus = Literal["open", "resolved", "expired"]


class PendingRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    request_id: str
    kind: PendingKind
    session_key: str
    payload: dict[str, JsonValue]
    status: PendingStatus = "open"
    answer: dict[str, JsonValue] | None = None
    webex_message_id: str | None = None
    created: datetime = Field(default_factory=utcnow)
    deadline: datetime


QueueKind = Literal["prompt", "steer"]


class QueuedMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    session_key: str
    kind: QueueKind
    text: str
    created: datetime
