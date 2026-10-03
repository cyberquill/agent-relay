"""Parse a chat message into a relay command. Shared by the Webex and PWA front ends."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from enum import StrEnum


class CommandName(StrEnum):
    NEW = "new"
    SESSIONS = "sessions"
    APPS = "apps"
    WS = "ws"
    MODELS = "models"
    MODEL = "model"
    STOP = "stop"
    STATUS = "status"
    DIFF = "diff"
    AWAY = "away"
    BACK = "back"
    DOCTOR = "doctor"
    PANIC = "panic"
    HELP = "help"


THREAD_ONLY = frozenset({CommandName.MODEL, CommandName.STOP, CommandName.STATUS, CommandName.DIFF})

HELP_TEXT = """**agent-relay commands**
- `/new [app] [ws] [model] ["prompt"]` start a session (missing pieces are asked for)
- `/sessions` list sessions · `/apps` list apps · `/ws` list workspaces · `/models <app>` list models
- In a session thread: `/model [m]` · `/stop` · `/status` · `/diff` · `/back` · plain text is a prompt
- `/away [on|off]` in a thread or globally · `/doctor` health · `/panic` cancel everything and pause
- `/help` this list"""

HELP_HINT = (
    "Reply inside a session thread to talk to it, or send `/new` to start one. `/help` lists commands."
)


@dataclass(frozen=True)
class Command:
    name: CommandName
    args: tuple[str, ...] = ()
    prompt: str | None = None


@dataclass(frozen=True)
class Prompt:
    text: str


@dataclass(frozen=True)
class HelpHint:
    pass


@dataclass(frozen=True)
class Unknown:
    name: str


@dataclass(frozen=True)
class Invalid:
    name: CommandName
    reason: str
    args: tuple[str, ...] = field(default_factory=tuple)


type Parsed = Command | Prompt | HelpHint | Unknown | Invalid

_SMART_QUOTES = str.maketrans({"\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'"})
_QUOTED = re.compile(r"""(?P<q>["'])(?P<body>.*?)(?P=q)""", re.DOTALL)
_ALIASES = {"workspaces": CommandName.WS, "session": CommandName.SESSIONS, "?": CommandName.HELP}


def _split(text: str) -> list[str]:
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def _parse_new(rest: str) -> Command | Invalid:
    prompt: str | None = None
    match = _QUOTED.search(rest)
    if match:
        prompt = match.group("body").strip() or None
        rest = rest[: match.start()] + " " + rest[match.end() :]
    tokens = _split(rest)
    positional, extra = tokens[:3], tokens[3:]
    if extra:
        if prompt is not None:
            return Invalid(CommandName.NEW, "too many arguments: put the prompt in quotes", tuple(tokens))
        prompt = " ".join(extra)
    return Command(CommandName.NEW, tuple(positional), prompt)


def parse(text: str, in_thread: bool) -> Parsed:
    stripped = text.translate(_SMART_QUOTES).strip()
    if not stripped:
        return HelpHint()
    if not stripped.startswith("/"):
        return Prompt(stripped) if in_thread else HelpHint()
    head, _, rest = stripped[1:].partition(" ")
    if not head:
        return Prompt(stripped) if in_thread else HelpHint()
    word = head.lower()
    name = _ALIASES.get(word)
    if name is None:
        try:
            name = CommandName(word)
        except ValueError:
            if "/" in word or "." in word:
                return Prompt(stripped) if in_thread else HelpHint()
            return Unknown(word)
    rest = rest.strip()
    if name is CommandName.NEW:
        return _parse_new(rest)
    if name in THREAD_ONLY and not in_thread:
        return Invalid(name, f"`/{name.value}` only works inside a session thread")
    args = tuple(_split(rest))
    if name is CommandName.MODELS and len(args) != 1:
        return Invalid(name, "usage: `/models <app>`", args)
    if name is CommandName.MODEL and len(args) > 1:
        return Invalid(name, "usage: `/model [model]`", args)
    if name is CommandName.AWAY:
        if len(args) > 1 or (args and args[0].lower() not in ("on", "off")):
            return Invalid(name, "usage: `/away [on|off]`", args)
        return Command(name, tuple(a.lower() for a in args))
    if name in (CommandName.SESSIONS, CommandName.APPS, CommandName.WS) and args:
        return Invalid(name, f"`/{name.value}` takes no arguments", args)
    return Command(name, args)
