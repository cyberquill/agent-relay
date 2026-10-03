"""Map OpenCode server events to small typed records. Pure: no I/O, no registry.

Text is streamed from `message.part.delta` on assistant text parts. When a provider does not stream,
the remainder of a part's final text is emitted from `message.part.updated`, so nothing is lost or
sent twice. Reasoning parts are never forwarded.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from pydantic import JsonValue

from agent_relay.adapters.base import Decision
from agent_relay.adapters.opencode.client import ServerEvent

REPLY_DECISION: dict[str, Decision] = {"once": "allow", "always": "allow_session", "reject": "deny"}


@dataclass(frozen=True)
class Created:
    sid: str
    title: str | None


@dataclass(frozen=True)
class Delta:
    sid: str
    text: str


@dataclass(frozen=True)
class ToolStart:
    sid: str
    call_id: str
    name: str
    detail: str


@dataclass(frozen=True)
class ToolEnd:
    sid: str
    call_id: str
    name: str
    ok: bool
    summary: str


@dataclass(frozen=True)
class Busy:
    sid: str


@dataclass(frozen=True)
class Idle:
    sid: str
    text: str | None


@dataclass(frozen=True)
class Failed:
    sid: str
    message: str


@dataclass(frozen=True)
class PermissionAsk:
    sid: str
    request_id: str
    tool: str
    detail: str
    call_id: str | None


@dataclass(frozen=True)
class PermissionDone:
    sid: str
    request_id: str
    decision: Decision


@dataclass(frozen=True)
class QuestionAsk:
    sid: str
    request_id: str
    question: str
    options: tuple[str, ...]
    count: int


@dataclass(frozen=True)
class QuestionDone:
    sid: str
    request_id: str
    answer: str | None


type Record = (
    Created
    | Delta
    | ToolStart
    | ToolEnd
    | Busy
    | Idle
    | Failed
    | PermissionAsk
    | PermissionDone
    | QuestionAsk
    | QuestionDone
)


@dataclass
class _Turn:
    parts: dict[str, str] = field(default_factory=dict)
    streamed: dict[str, int] = field(default_factory=dict)
    tools: dict[str, str] = field(default_factory=dict)

    def text(self) -> str | None:
        joined = "\n\n".join(t.strip() for t in self.parts.values() if t.strip())
        return joined or None


def _str(value: JsonValue) -> str | None:
    return value if isinstance(value, str) and value else None


def _obj(value: JsonValue) -> dict[str, JsonValue]:
    return value if isinstance(value, dict) else {}


def _detail(value: JsonValue, limit: int = 300) -> str:
    if isinstance(value, dict):
        for key in ("command", "filePath", "path", "pattern", "url", "query"):
            found = _str(value.get(key))
            if found:
                return found[:limit]
    if value in (None, {}, ""):
        return ""
    return (value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))[:limit]


class EventMapper:
    def __init__(self) -> None:
        self._roles: dict[str, str] = {}
        self._part_types: dict[str, str] = {}
        self._turns: dict[str, _Turn] = {}

    def _turn(self, sid: str) -> _Turn:
        return self._turns.setdefault(sid, _Turn())

    def map(self, event: ServerEvent) -> list[Record]:
        props = event.properties
        sid = _str(props.get("sessionID")) or _str(_obj(props.get("info")).get("sessionID"))
        match event.type:
            case "session.created":
                info = _obj(props.get("info"))
                created = sid or _str(info.get("id"))
                return [Created(created, _str(info.get("title")))] if created else []
            case "message.updated":
                info = _obj(props.get("info"))
                mid, role = _str(info.get("id")), _str(info.get("role"))
                if mid and role:
                    self._roles[mid] = role
                return []
            case "message.part.updated":
                return self._part(sid, _obj(props.get("part")))
            case "message.part.delta":
                return self._delta(sid, props)
            case "session.status":
                status = _str(_obj(props.get("status")).get("type"))
                return [Busy(sid)] if sid and status == "busy" else []
            case "session.idle":
                if not sid:
                    return []
                turn = self._turns.pop(sid, _Turn())
                return [Idle(sid, turn.text())]
            case "session.error":
                error = _obj(props.get("error"))
                message = _str(_obj(error.get("data")).get("message")) or _str(error.get("name")) or "error"
                return [Failed(sid, message)] if sid else []
            case "permission.asked" | "permission.updated":
                return self._permission(sid, props)
            case "permission.replied":
                request = _str(props.get("requestID")) or _str(props.get("permissionID"))
                reply = _str(props.get("reply")) or _str(props.get("response"))
                if sid and request and reply in REPLY_DECISION:
                    return [PermissionDone(sid, request, REPLY_DECISION[reply])]
                return []
            case "question.asked":
                return self._question(sid, props)
            case "question.replied" | "question.rejected":
                request = _str(props.get("requestID")) or _str(props.get("id"))
                if not (sid and request):
                    return []
                answers = props.get("answers")
                flat = [
                    a
                    for group in (answers if isinstance(answers, list) else [])
                    if isinstance(group, list)
                    for a in group
                    if isinstance(a, str)
                ]
                answer = ", ".join(flat) if event.type == "question.replied" and flat else None
                return [QuestionDone(sid, request, answer)]
        return []

    def _assistant(self, mid: str | None) -> bool:
        return mid is not None and self._roles.get(mid) == "assistant"

    def _part(self, sid: str | None, part: dict[str, JsonValue]) -> list[Record]:
        sid = sid or _str(part.get("sessionID"))
        pid, kind, mid = _str(part.get("id")), _str(part.get("type")), _str(part.get("messageID"))
        if not (sid and pid and kind):
            return []
        self._part_types[pid] = kind
        if kind == "text" and self._assistant(mid) and part.get("synthetic") is not True:
            text = part.get("text") if isinstance(part.get("text"), str) else ""
            assert isinstance(text, str)
            turn = self._turn(sid)
            turn.parts[pid] = text
            sent = turn.streamed.get(pid, 0)
            if len(text) > sent:
                turn.streamed[pid] = len(text)
                return [Delta(sid, text[sent:])]
            return []
        if kind == "tool":
            return self._tool(sid, part)
        return []

    def _delta(self, sid: str | None, props: dict[str, JsonValue]) -> list[Record]:
        pid, mid, delta = _str(props.get("partID")), _str(props.get("messageID")), _str(props.get("delta"))
        if not (sid and pid and delta) or props.get("field") != "text":
            return []
        if self._part_types.get(pid) != "text" or not self._assistant(mid):
            return []
        turn = self._turn(sid)
        turn.parts[pid] = turn.parts.get(pid, "") + delta
        turn.streamed[pid] = turn.streamed.get(pid, 0) + len(delta)
        return [Delta(sid, delta)]

    def _tool(self, sid: str, part: dict[str, JsonValue]) -> list[Record]:
        call_id = _str(part.get("callID")) or _str(part.get("id"))
        name = _str(part.get("tool")) or "tool"
        state = _obj(part.get("state"))
        status = _str(state.get("status")) or "pending"
        if not call_id:
            return []
        turn = self._turn(sid)
        previous = turn.tools.get(call_id)
        turn.tools[call_id] = status
        if status in ("pending", "running") and previous is None:
            return [ToolStart(sid, call_id, name, _detail(state.get("input")))]
        if status in ("completed", "error") and previous not in ("completed", "error"):
            out: list[Record] = []
            if previous is None:
                out.append(ToolStart(sid, call_id, name, _detail(state.get("input"))))
            summary = _str(state.get("title")) or _str(state.get("error")) or ""
            out.append(ToolEnd(sid, call_id, name, status == "completed", summary[:300]))
            return out
        return []

    def _permission(self, sid: str | None, props: dict[str, JsonValue]) -> list[Record]:
        request = _str(props.get("id"))
        if not (sid and request):
            return []
        tool = _str(props.get("permission")) or _str(props.get("type")) or "permission"
        patterns = props.get("patterns") if isinstance(props.get("patterns"), list) else props.get("pattern")
        if isinstance(patterns, list):
            detail = ", ".join(p for p in patterns if isinstance(p, str))
        else:
            detail = _str(patterns) or _str(props.get("title")) or ""
        call = _obj(props.get("tool"))
        call_id = _str(call.get("callID")) or _str(props.get("callID"))
        return [PermissionAsk(sid, request, tool, f"{tool}: {detail}" if detail else tool, call_id)]

    def _question(self, sid: str | None, props: dict[str, JsonValue]) -> list[Record]:
        request = _str(props.get("id"))
        questions = props.get("questions")
        items = [_obj(q) for q in questions] if isinstance(questions, list) else []
        if not (sid and request and items):
            return []
        texts: list[str] = []
        for item in items:
            header = _str(item.get("header"))
            body = _str(item.get("question")) or ""
            texts.append(f"{header}: {body}" if header else body)
        options: tuple[str, ...] = ()
        if len(items) == 1:
            raw = items[0].get("options")
            options = tuple(
                label
                for label in (_str(_obj(o).get("label")) for o in (raw if isinstance(raw, list) else []))
                if label
            )
            question = texts[0]
        else:
            numbered = "\n".join(f"{n}. {t}" for n, t in enumerate(texts, 1))
            question = f"{numbered}\n\nReply with one answer per line, in order."
        return [QuestionAsk(sid, request, question, options, len(items))]
