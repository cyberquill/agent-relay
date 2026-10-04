"""ACP messages from `cursor-agent acp` to the relay's event model, and phone answers back to ACP."""

from __future__ import annotations

import json
from dataclasses import dataclass

from pydantic import JsonValue

from agent_relay.adapters.base import Decision

type Params = dict[str, JsonValue]

DETAIL_CLIP = 300
PLAN_CLIP = 1_500
MULTI_HINT = (
    "Answer one line per question; use option numbers, ids or labels (comma-separated when several apply)."
)
OPTION_KINDS: dict[str, tuple[str, str]] = {
    "allow": ("allow_once", "allow-once"),
    "allow_session": ("allow_always", "allow-always"),
    "deny": ("reject_once", "reject-once"),
}


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def _str(value: JsonValue, fallback: str = "") -> str:
    return value if isinstance(value, str) else fallback


def _detail(raw: JsonValue, title: str) -> str:
    """A tool's input for the phone: a lone string argument as itself (blank when it is the title)."""
    if raw is None or raw == {}:
        return ""
    if isinstance(raw, dict) and len(raw) == 1:
        (value,) = raw.values()
        if isinstance(value, str):
            return "" if value == title else _clip(value, DETAIL_CLIP)
    return _clip(json.dumps(raw, default=str), DETAIL_CLIP)


def _dicts(value: JsonValue) -> list[Params]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


@dataclass(frozen=True)
class Delta:
    text: str


@dataclass(frozen=True)
class ToolStart:
    tool_id: str
    name: str
    detail: str


@dataclass(frozen=True)
class ToolEnd:
    tool_id: str
    name: str
    ok: bool
    summary: str


type Mapped = Delta | ToolStart | ToolEnd


def _content_text(content: JsonValue) -> str:
    if isinstance(content, dict):
        if content.get("type") == "text":
            return _str(content.get("text"))
        return _content_text(content.get("content"))
    if isinstance(content, list):
        return "".join(_content_text(c) for c in content)
    return ""


def map_update(update: Params) -> list[Mapped]:
    kind = update.get("sessionUpdate")
    if kind == "agent_message_chunk":
        text = _content_text(update.get("content"))
        return [Delta(text)] if text else []
    tool_id = _str(update.get("toolCallId"))
    if not tool_id:
        return []
    name = _str(update.get("title")) or _str(update.get("kind"), "tool")
    status = update.get("status")
    mapped: list[Mapped] = []
    if kind == "tool_call":
        mapped.append(ToolStart(tool_id, name, _detail(update.get("rawInput"), name)))
    if kind in ("tool_call", "tool_call_update") and status in ("completed", "failed"):
        summary = _clip(_content_text(update.get("content")), DETAIL_CLIP)
        mapped.append(ToolEnd(tool_id, name, status == "completed", summary))
    return mapped


# permissions


def describe_permission(params: Params) -> tuple[str, str]:
    call = params.get("toolCall")
    call = call if isinstance(call, dict) else {}
    tool = _str(call.get("title")) or _str(call.get("kind"), "tool")
    return tool, _detail(call.get("rawInput"), tool)


def pick_option(options: JsonValue, decision: Decision) -> str:
    """The option id that means `decision`: by ACP kind first, then by Cursor's documented ids."""
    kind, fallback = OPTION_KINDS[decision]
    offered = _dicts(options)
    for option in offered:
        if option.get("kind") == kind:
            return _str(option.get("optionId"), fallback)
    for option in offered:
        if option.get("optionId") == fallback:
            return fallback
    if decision == "deny":
        for option in offered:
            if _str(option.get("kind")).startswith("reject"):
                return _str(option.get("optionId"), fallback)
    return fallback


def permission_result(options: JsonValue, decision: Decision | None) -> Params:
    if decision is None:
        return {"outcome": {"outcome": "cancelled"}}
    return {"outcome": {"outcome": "selected", "optionId": pick_option(options, decision)}}


# cursor/ask_question


def questions_of(params: Params) -> list[Params]:
    return _dicts(params.get("questions"))


def _options(question: Params) -> list[Params]:
    return _dicts(question.get("options"))


def describe_questions(params: Params) -> tuple[str, tuple[str, ...]]:
    """Text for the phone and, for a single question, its option labels as buttons."""
    questions = questions_of(params)
    title = _str(params.get("title"))
    if len(questions) == 1:
        (only,) = questions
        prompt = _str(only.get("prompt"), "Cursor has a question")
        labels = tuple(_str(o.get("label")) or _str(o.get("id")) for o in _options(only))
        if only.get("allowMultiple") is True:
            prompt = f"{prompt} (several allowed: comma-separated)"
        return (f"{title}\n{prompt}" if title else prompt), labels
    lines = [title] if title else []
    for number, question in enumerate(questions, start=1):
        listed = ", ".join(
            f"{i}. {_str(o.get('label')) or _str(o.get('id'))}"
            for i, o in enumerate(_options(question), start=1)
        )
        lines.append(f"{number}) {_str(question.get('prompt'))} [{listed}]")
    lines.append(MULTI_HINT)
    return "\n".join(lines), ()


def _resolve(question: Params, token: str) -> str | None:
    options = _options(question)
    cleaned = token.strip()
    if cleaned.isdigit() and 1 <= int(cleaned) <= len(options):
        return _str(options[int(cleaned) - 1].get("id")) or None
    lowered = cleaned.lower()
    for option in options:
        if lowered in (_str(option.get("id")).lower(), _str(option.get("label")).lower()):
            return _str(option.get("id")) or None
    return None


def question_result(params: Params, answer: str | None) -> Params:
    """Map a phone answer onto option ids; text that names no option leaves that question unanswered."""
    if answer is None:
        return {"outcome": {"outcome": "skipped", "reason": "no answer from the phone"}}
    questions = questions_of(params)
    lines = [answer] if len(questions) == 1 else [line for line in answer.splitlines() if line.strip()]
    answers: list[JsonValue] = []
    for index, question in enumerate(questions):
        line = lines[index] if index < len(lines) else ""
        tokens = line.split(",") if question.get("allowMultiple") is True else [line]
        selected = [s for s in (_resolve(question, t) for t in tokens) if s]
        answers.append(
            {"questionId": _str(question.get("id")), "selectedOptionIds": list(dict.fromkeys(selected))}
        )
    return {"outcome": {"outcome": "answered", "answers": answers}}


# cursor/create_plan


def describe_plan(params: Params) -> str:
    parts = [_str(params.get("name")), _str(params.get("overview")), _str(params.get("plan"))]
    return _clip("\n\n".join(p for p in parts if p), PLAN_CLIP)


def plan_result(decision: Decision | None) -> Params:
    if decision is None:
        return {"outcome": {"outcome": "cancelled"}}
    if decision == "deny":
        return {"outcome": {"outcome": "rejected", "reason": "rejected from the phone"}}
    return {"outcome": {"outcome": "accepted"}}
