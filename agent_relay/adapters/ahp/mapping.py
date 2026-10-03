"""Pure mapping from AHP actions to adapter records, and from phone answers to AHP input answers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import JsonValue, ValidationError

from agent_relay.adapters.ahp import models as m
from agent_relay.log import get_logger

log = get_logger(__name__)

MULTI_ANSWER_HINT = "Reply with one answer per line, in order."
DETAIL_LIMIT = 200
type InputResponse = Literal["accept", "decline", "cancel"]

YES = frozenset({"y", "yes", "true", "1", "ok", "allow", "accept"})
NO = frozenset({"n", "no", "false", "0", "deny", "decline"})


@dataclass(frozen=True)
class TurnStarted:
    chat: str
    turn_id: str


@dataclass(frozen=True)
class Delta:
    chat: str
    text: str


@dataclass(frozen=True)
class ToolStart:
    chat: str
    call_id: str
    name: str
    detail: str


@dataclass(frozen=True)
class ToolAsk:
    chat: str
    turn_id: str
    call_id: str
    tool: str
    detail: str
    options: tuple[m.ConfirmationOption, ...] = ()


@dataclass(frozen=True)
class ToolConfirmed:
    chat: str
    call_id: str
    approved: bool


@dataclass(frozen=True)
class ToolEnd:
    chat: str
    call_id: str
    name: str
    ok: bool
    summary: str


@dataclass(frozen=True)
class InputAsk:
    chat: str
    request_id: str
    question: str
    options: tuple[str, ...]
    questions: tuple[m.ChatInputQuestion, ...]


@dataclass(frozen=True)
class InputDone:
    chat: str
    request_id: str
    answer: str | None


@dataclass(frozen=True)
class TurnDone:
    chat: str
    turn_id: str
    text: str | None


@dataclass(frozen=True)
class TurnCancelled:
    chat: str
    turn_id: str


@dataclass(frozen=True)
class Failed:
    chat: str
    turn_id: str
    message: str


@dataclass(frozen=True)
class ChatAdded:
    session: str
    chat: str


@dataclass(frozen=True)
class TitleChanged:
    session: str
    title: str


type Record = (
    TurnStarted
    | Delta
    | ToolStart
    | ToolAsk
    | ToolConfirmed
    | ToolEnd
    | InputAsk
    | InputDone
    | TurnDone
    | TurnCancelled
    | Failed
    | ChatAdded
    | TitleChanged
)


def text_of(value: JsonValue) -> str:
    """StringOrMarkdown, ToolInput or anything else, as plain text."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("markdown", "value", "text", "uri"):
            inner = value.get(key)
            if isinstance(inner, str):
                return inner
    return ""


def _clip(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= DETAIL_LIMIT else f"{flat[: DETAIL_LIMIT - 1]}…"


def _choices(question: m.ChatInputQuestion) -> tuple[str, ...]:
    if isinstance(question, m.ChatInputSingleSelectQuestion | m.ChatInputMultiSelectQuestion):
        return tuple(option.label for option in question.options)
    if isinstance(question, m.ChatInputBooleanQuestion):
        return ("yes", "no")
    return ()


def describe_request(request: m.ChatInputRequest) -> tuple[str, tuple[str, ...]]:
    """The phone text and the quick-reply options for an input request."""
    questions = request.questions or []
    if not questions:
        text = "\n".join(p for p in (request.message, request.url) if p) or "The agent needs your input."
        return text, ("accept", "decline")
    if len(questions) == 1:
        only = questions[0]
        lead = f"{request.message}\n" if request.message and request.message != only.message else ""
        return f"{lead}{only.message}", _choices(only)
    lines = [request.message] if request.message else []
    for index, question in enumerate(questions, 1):
        choices = _choices(question)
        suffix = f" ({' / '.join(choices)})" if choices else ""
        lines.append(f"{index}. {question.message}{suffix}")
    lines.append(MULTI_ANSWER_HINT)
    return "\n".join(lines), ()


def _pick(options: list[m.ChatInputOption], raw: str) -> str | None:
    wanted = raw.strip().lower()
    if wanted.isdigit() and 1 <= int(wanted) <= len(options):
        return options[int(wanted) - 1].id
    for option in options:
        if wanted in (option.id.lower(), option.label.lower()):
            return option.id
    return None


def _answer_one(question: m.ChatInputQuestion, raw: str) -> m.ChatInputAnswer:
    skipped = m.ChatInputSkipped(state="skipped", freeform_values=[raw] if raw else None)
    value: m.ChatInputAnswerValue
    if isinstance(question, m.ChatInputTextQuestion):
        value = m.ChatInputTextAnswerValue(kind="text", value=raw)
    elif isinstance(question, m.ChatInputNumberQuestion):
        try:
            number = int(raw) if question.kind == "integer" else float(raw)
        except ValueError:
            return skipped
        value = m.ChatInputNumberAnswerValue(kind="number", value=number)
    elif isinstance(question, m.ChatInputBooleanQuestion):
        lowered = raw.strip().lower()
        if lowered not in YES | NO:
            return skipped
        value = m.ChatInputBooleanAnswerValue(kind="boolean", value=lowered in YES)
    elif isinstance(question, m.ChatInputSingleSelectQuestion):
        picked = _pick(question.options, raw)
        if picked is not None:
            value = m.ChatInputSelectedAnswerValue(kind="selected", value=picked)
        elif question.allow_freeform_input:
            value = m.ChatInputSelectedAnswerValue(kind="selected", value="", freeform_values=[raw])
        else:
            return skipped
    else:
        chosen: list[str] = []
        free: list[str] = []
        for piece in (p.strip() for p in raw.split(",")):
            if not piece:
                continue
            picked = _pick(question.options, piece)
            if picked is not None:
                chosen.append(picked)
            else:
                free.append(piece)
        if free and not question.allow_freeform_input:
            return skipped
        value = m.ChatInputSelectedManyAnswerValue(
            kind="selected-many", value=chosen, freeform_values=free or None
        )
    return m.ChatInputAnswered(state="submitted", value=value)


def build_answers(
    questions: tuple[m.ChatInputQuestion, ...], answer: str | None
) -> tuple[InputResponse, dict[str, m.ChatInputAnswer] | None]:
    """The `chat/inputCompleted` response kind and answers for a phone reply (None means skip)."""
    if answer is None:
        return "decline", None
    if not questions:
        return ("decline" if answer.strip().lower() in NO else "accept"), None
    if len(questions) == 1:
        lines = [answer.strip()]
    else:
        lines = [line.strip() for line in answer.splitlines() if line.strip()]
    answers: dict[str, m.ChatInputAnswer] = {}
    for index, question in enumerate(questions):
        raw = lines[index] if index < len(lines) else ""
        answers[question.id] = _answer_one(question, raw) if raw else m.ChatInputSkipped(state="skipped")
    return "accept", answers


def summarize_answers(answers: dict[str, JsonValue] | None) -> str | None:
    """A desk answer as text for the phone."""
    if not answers:
        return None
    parts: list[str] = []
    for entry in answers.values():
        if not isinstance(entry, dict):
            continue
        value = entry.get("value")
        inner = value.get("value") if isinstance(value, dict) else None
        if isinstance(inner, list):
            parts.append(", ".join(str(v) for v in inner))
        elif inner is not None:
            parts.append(str(inner))
    return "\n".join(parts) or None


@dataclass
class _ChatState:
    parts: dict[str, str] = field(default_factory=dict)
    tools: dict[str, str] = field(default_factory=dict)


class ActionMapper:
    """Turns `action` notifications into records. Only markdown parts count as reply text; reasoning,
    tool and system parts are not forwarded."""

    def __init__(self) -> None:
        self._chats: dict[str, _ChatState] = {}

    def _chat(self, chat: str) -> _ChatState:
        return self._chats.setdefault(chat, _ChatState())

    def forget(self, chat: str) -> None:
        self._chats.pop(chat, None)

    def map(self, channel: str, action: dict[str, JsonValue]) -> list[Record]:
        kind = action.get("type")
        if kind == "chat/toolCallConfirmed":
            call_id = action.get("toolCallId")
            if isinstance(call_id, str):
                return [ToolConfirmed(channel, call_id, action.get("approved") is True)]
            return []
        model_type = m.ACTION_MODELS.get(kind) if isinstance(kind, str) else None
        if model_type is None:
            return []
        try:
            parsed = model_type.model_validate(action)
        except ValidationError as exc:
            log.warning(f"agent host sent a {kind} this relay cannot read ({exc.error_count()} errors)")
            return []
        return self._map(channel, parsed)

    def _map(self, channel: str, action: m.AhpModel) -> list[Record]:
        state = self._chat(channel)
        match action:
            case m.ChatTurnStartedAction(turn_id=turn_id):
                state.parts.clear()
                return [TurnStarted(channel, turn_id)]
            case m.ChatResponsePartAction(part=m.MarkdownResponsePart(id=part_id, content=content)):
                state.parts[part_id] = content
                return [Delta(channel, content)] if content else []
            case m.ChatResponsePartAction(turn_id=turn_id, part=m.ErrorResponsePart(error=error)):
                return [Failed(channel, turn_id, error.message)]
            case m.ChatDeltaAction(part_id=part_id, content=content):
                if part_id not in state.parts or not content:
                    return []
                state.parts[part_id] += content
                return [Delta(channel, content)]
            case m.ChatToolCallStartAction(tool_call_id=call_id, tool_name=name, display_name=label):
                state.tools[call_id] = label or name
                return [ToolStart(channel, call_id, label or name, "")]
            case m.ChatToolCallReadyAction() if action.confirmed is None:
                tool = state.tools.get(action.tool_call_id, action.tool_call_id)
                detail = text_of(action.invocation_message) or text_of(action.tool_input)
                return [
                    ToolAsk(
                        channel,
                        action.turn_id,
                        action.tool_call_id,
                        tool,
                        _clip(detail),
                        tuple(action.options or ()),
                    )
                ]
            case m.ChatToolCallCompleteAction(tool_call_id=call_id, result=result):
                name = state.tools.pop(call_id, call_id)
                failure = result.error.message if result.error else ""
                summary = failure if not result.success else text_of(result.past_tense_message)
                return [ToolEnd(channel, call_id, name, result.success, _clip(summary))]
            case m.ChatInputRequestedAction(request=request):
                question, options = describe_request(request)
                return [InputAsk(channel, request.id, question, options, tuple(request.questions or ()))]
            case m.ChatInputCompletedAction(request_id=request_id, response=response, answers=answers):
                text = None
                if response == "accept":
                    raw: dict[str, JsonValue] = {
                        k: v.model_dump(mode="json", by_alias=True) for k, v in (answers or {}).items()
                    }
                    text = summarize_answers(raw) or "accepted"
                return [InputDone(channel, request_id, text)]
            case m.ChatTurnCompleteAction(turn_id=turn_id):
                text = "\n\n".join(t for t in state.parts.values() if t.strip()) or None
                state.parts.clear()
                return [TurnDone(channel, turn_id, text)]
            case m.ChatTurnCancelledAction(turn_id=turn_id):
                state.parts.clear()
                return [TurnCancelled(channel, turn_id)]
            case m.ChatErrorAction(turn_id=turn_id, part=part):
                state.parts.clear()
                return [Failed(channel, turn_id, part.error.message)]
            case m.SessionChatAddedAction(summary=chat_summary):
                resource = chat_summary.get("resource") if isinstance(chat_summary, dict) else None
                return [ChatAdded(channel, resource)] if isinstance(resource, str) else []
            case m.SessionTitleChangedAction(title=title):
                return [TitleChanged(channel, title)] if title else []
        return []
