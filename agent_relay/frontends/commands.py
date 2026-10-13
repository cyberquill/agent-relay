"""Parse a chat message into a relay command. Shared by the Webex and PWA front ends.

The command names, their argument rules and their scopes come from `frontends.registry`.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field

from agent_relay.frontends import registry

HELP_HINT = (
    "Reply inside a session thread to talk to it, or send `/new` to start one. `/help` lists commands."
)


@dataclass(frozen=True)
class Command:
    name: str
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
    name: str
    reason: str
    args: tuple[str, ...] = field(default_factory=tuple)


type Parsed = Command | Prompt | HelpHint | Unknown | Invalid

_SMART_QUOTES = str.maketrans({"\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'"})


def split_args(text: str) -> list[str]:
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def _usage(spec: registry.CommandSpec) -> str:
    if spec.max_args == 0:
        return f"`/{spec.name}` takes no arguments"
    return f"usage: `{spec.usage or f'/{spec.name}'}`"


def _check_args(spec: registry.CommandSpec, args: tuple[str, ...]) -> Command | Invalid:
    lowered = tuple(a.lower() for a in args) if spec.choices else args
    if spec.choices and lowered and lowered[0] not in spec.choices:
        return Invalid(spec.name, _usage(spec), args)
    if len(lowered) < spec.min_args or (spec.max_args is not None and len(lowered) > spec.max_args):
        return Invalid(spec.name, _usage(spec), args)
    return Command(spec.name, lowered)


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
    spec = registry.lookup(word)
    if spec is None:
        if "/" in word or "." in word:
            return Prompt(stripped) if in_thread else HelpHint()
        return Unknown(word)
    if spec.scope == "thread" and not in_thread:
        return Invalid(spec.name, f"`/{spec.name}` only works inside a session thread")
    if spec.scope == "top" and in_thread:
        return Invalid(spec.name, f"`/{spec.name}` only works outside a session thread")
    rest = rest.strip()
    if spec.parse is not None:
        return spec.parse(rest)
    return _check_args(spec, tuple(split_args(rest)))
