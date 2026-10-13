"""The normalised event model every adapter and hook shim produces."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class Harness(StrEnum):
    VSCODE_COPILOT = "vscode_copilot"
    VSCODE = "vscode"
    COPILOT_CLI = "copilot_cli"
    CURSOR = "cursor"
    CURSOR_CLI = "cursor_cli"
    CURSOR_CDP = "cursor_cdp"
    OPENCODE = "opencode"


class Origin(StrEnum):
    DESK = "desk"
    PHONE = "phone"


def utcnow() -> datetime:
    return datetime.now(UTC)


class _Base(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    harness: Harness
    session_key: str
    workspace: str | None = None
    ts: datetime = Field(default_factory=utcnow)
    origin: Origin = Origin.DESK


class SessionStarted(_Base):
    type: Literal["session.started"] = "session.started"
    title: str | None = None
    model: str | None = None


class TextDelta(_Base):
    type: Literal["text.delta"] = "text.delta"
    text: str


class ToolStarted(_Base):
    type: Literal["tool.started"] = "tool.started"
    tool_id: str
    name: str
    detail: str = ""


class ToolFinished(_Base):
    type: Literal["tool.finished"] = "tool.finished"
    tool_id: str
    name: str
    ok: bool = True
    summary: str = ""


class ApprovalRequested(_Base):
    type: Literal["approval.requested"] = "approval.requested"
    request_id: str
    tool: str
    detail: str = ""


type ResolvedBy = Literal["phone", "deadline", "panic", "desk", "ended", "stopped", "abandoned"]


class ApprovalResolved(_Base):
    type: Literal["approval.resolved"] = "approval.resolved"
    request_id: str
    decision: Literal["allow", "allow_session", "deny", "desk"]
    by: ResolvedBy = "phone"


class QuestionAsked(_Base):
    type: Literal["question.asked"] = "question.asked"
    request_id: str
    question: str
    options: tuple[str, ...] = ()


class QuestionAnswered(_Base):
    type: Literal["question.answered"] = "question.answered"
    request_id: str
    answer: str | None
    by: ResolvedBy = "phone"


class TurnCompleted(_Base):
    type: Literal["turn.completed"] = "turn.completed"
    text: str | None = None


class SessionParked(_Base):
    type: Literal["session.parked"] = "session.parked"
    deadline: datetime


class SessionReleased(_Base):
    type: Literal["session.released"] = "session.released"
    reason: str


class ErrorEvent(_Base):
    type: Literal["error"] = "error"
    message: str


type Event = Annotated[
    SessionStarted
    | TextDelta
    | ToolStarted
    | ToolFinished
    | ApprovalRequested
    | ApprovalResolved
    | QuestionAsked
    | QuestionAnswered
    | TurnCompleted
    | SessionParked
    | SessionReleased
    | ErrorEvent,
    Field(discriminator="type"),
]

EVENT_TYPES: tuple[str, ...] = (
    "session.started",
    "text.delta",
    "tool.started",
    "tool.finished",
    "approval.requested",
    "approval.resolved",
    "question.asked",
    "question.answered",
    "turn.completed",
    "session.parked",
    "session.released",
    "error",
)
