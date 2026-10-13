"""The command table. A command registers itself in its own module; the parser, the dispatcher
and `/help` all read this table, so adding a command never means editing a central switch.

Modules under `agent_relay/frontends/webex/commands/` are imported on first lookup, and each one
calls `register(CommandSpec(...))` at import time.
"""

from __future__ import annotations

import importlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from agent_relay.frontends.commands import Command, Invalid
    from agent_relay.frontends.webex.handlers import Context, Handlers

type Scope = Literal["top", "thread", "both"]
type CommandHandler = Callable[[Handlers, Context, Command], Awaitable[None]]
type RestParser = Callable[[str], Command | Invalid]

COMMANDS_PACKAGE = "agent_relay.frontends.webex.commands"


@dataclass(frozen=True)
class CommandSpec:
    """One command. `min_args`/`max_args` bound the words after the name (`max_args=None` ignores extras).

    `choices` limits the first argument and lowercases the arguments. `parse` replaces the generic
    word splitting for commands with their own grammar. `order` places the command in `/help`.
    """

    name: str
    handler: CommandHandler
    help: str
    scope: Scope = "both"
    usage: str = ""
    aliases: tuple[str, ...] = ()
    min_args: int = 0
    max_args: int | None = None
    choices: tuple[str, ...] = ()
    parse: RestParser | None = None
    order: int = 100


_SPECS: dict[str, CommandSpec] = {}
_WORDS: dict[str, str] = {}
_loaded = False


def register(spec: CommandSpec) -> CommandSpec:
    words = (spec.name, *spec.aliases)
    taken = [w for w in words if w in _WORDS]
    if taken:
        raise ValueError(f"command word(s) already registered: {', '.join(taken)}")
    _SPECS[spec.name] = spec
    for word in words:
        _WORDS[word] = spec.name
    return spec


def unregister(name: str) -> None:
    """Remove a command (for tests that register a throwaway one)."""
    spec = _SPECS.pop(name, None)
    if spec is not None:
        for word in (spec.name, *spec.aliases):
            _WORDS.pop(word, None)


def ensure_loaded() -> None:
    global _loaded
    if not _loaded:
        _loaded = True
        importlib.import_module(COMMANDS_PACKAGE)


def lookup(word: str) -> CommandSpec | None:
    ensure_loaded()
    name = _WORDS.get(word)
    return _SPECS[name] if name is not None else None


def specs() -> list[CommandSpec]:
    ensure_loaded()
    return sorted(_SPECS.values(), key=lambda s: s.order)


def names() -> frozenset[str]:
    ensure_loaded()
    return frozenset(_SPECS)


def help_text() -> str:
    lines = ["**agent-relay commands**"]
    for spec in specs():
        where = " (in a session thread)" if spec.scope == "thread" else ""
        lines.append(f"- `{spec.usage or f'/{spec.name}'}` {spec.help}{where}")
    lines.append("- plain text in a session thread is a prompt")
    return "\n".join(lines)
