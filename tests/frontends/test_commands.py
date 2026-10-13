"""AC-11: table tests for the command parser."""

from __future__ import annotations

from enum import StrEnum

import pytest

from agent_relay.frontends import registry
from agent_relay.frontends.commands import (
    Command,
    HelpHint,
    Invalid,
    Parsed,
    Prompt,
    Unknown,
    parse,
)


class N(StrEnum):
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
    QUEUE = "queue"
    STEER = "steer"
    DIGEST = "digest"


TABLE: list[tuple[str, bool, Parsed]] = [
    # plain text
    ("hello there", True, Prompt("hello there")),
    ("hello there", False, HelpHint()),
    ("   ", True, HelpHint()),
    ("/Users/dev/app.py looks wrong", True, Prompt("/Users/dev/app.py looks wrong")),
    ("/Users/dev/app.py", False, HelpHint()),
    ("/", True, Prompt("/")),
    # /new
    ("/new", False, Command(N.NEW, (), None)),
    ("/new opencode", False, Command(N.NEW, ("opencode",), None)),
    ("/new opencode proj", False, Command(N.NEW, ("opencode", "proj"), None)),
    ("/new opencode proj m1", False, Command(N.NEW, ("opencode", "proj", "m1"), None)),
    (
        '/new opencode proj m1 "fix the tests"',
        False,
        Command(N.NEW, ("opencode", "proj", "m1"), "fix the tests"),
    ),
    ('/new opencode proj "fix it"', False, Command(N.NEW, ("opencode", "proj"), "fix it")),
    (
        "/new opencode proj m1 fix the tests",
        False,
        Command(N.NEW, ("opencode", "proj", "m1"), "fix the tests"),
    ),
    (
        "/new copilot proj \u201csmart quotes\u201d",
        False,
        Command(N.NEW, ("copilot", "proj"), "smart quotes"),
    ),
    ("/new cursor proj 'single'", True, Command(N.NEW, ("cursor", "proj"), "single")),
    (
        '/new a b c d "e"',
        False,
        Invalid(N.NEW, "too many arguments: put the prompt in quotes", ("a", "b", "c", "d")),
    ),
    ('/new oc proj ""', False, Command(N.NEW, ("oc", "proj"), None)),
    # an apostrophe inside a word is not a quote
    ("/new oc proj fix don't and can't", False, Command(N.NEW, ("oc", "proj", "fix"), "don't and can't")),
    ('/new oc proj "don\'t stop"', False, Command(N.NEW, ("oc", "proj"), "don't stop")),
    ("/new oc proj 'it's here'", False, Command(N.NEW, ("oc", "proj"), "it's here")),
    ('/new oc proj it\'s "quoted one"', False, Command(N.NEW, ("oc", "proj", "it's"), "quoted one")),
    ("/new oc proj don\u2019t panic", False, Command(N.NEW, ("oc", "proj", "don't"), "panic")),
    ("/NEW OpenCode", False, Command(N.NEW, ("OpenCode",), None)),
    # listings
    ("/sessions", False, Command(N.SESSIONS)),
    ("/session", True, Command(N.SESSIONS)),
    ("/apps", False, Command(N.APPS)),
    ("/ws", False, Command(N.WS)),
    ("/workspaces", False, Command(N.WS)),
    ("/apps now", False, Invalid(N.APPS, "`/apps` takes no arguments", ("now",))),
    ("/models opencode", False, Command(N.MODELS, ("opencode",))),
    ("/models", False, Invalid(N.MODELS, "usage: `/models <app>`")),
    ("/models a b", False, Invalid(N.MODELS, "usage: `/models <app>`", ("a", "b"))),
    # thread-only
    ("/model", True, Command(N.MODEL)),
    ("/model gpt-5", True, Command(N.MODEL, ("gpt-5",))),
    ("/model a b", True, Invalid(N.MODEL, "usage: `/model [model]`", ("a", "b"))),
    ("/model gpt-5", False, Invalid(N.MODEL, "`/model` only works inside a session thread")),
    ("/stop", True, Command(N.STOP)),
    ("/stop", False, Invalid(N.STOP, "`/stop` only works inside a session thread")),
    ("/status", True, Command(N.STATUS)),
    ("/status", False, Invalid(N.STATUS, "`/status` only works inside a session thread")),
    ("/diff", True, Command(N.DIFF)),
    ("/diff", False, Invalid(N.DIFF, "`/diff` only works inside a session thread")),
    # queue modes and the digest
    ("/queue", True, Command(N.QUEUE)),
    ("/queue STEER", True, Command(N.QUEUE, ("steer",))),
    ("/queue clear", True, Command(N.QUEUE, ("clear",))),
    ("/queue maybe", True, Invalid(N.QUEUE, "usage: `/queue [queue|steer|interrupt|clear]`", ("maybe",))),
    ("/queue", False, Invalid(N.QUEUE, "`/queue` only works inside a session thread")),
    ("/steer use pytest", True, Command(N.STEER, (), "use pytest")),
    ("/steer", True, Invalid(N.STEER, "usage: `/steer <message>`")),
    ("/steer x", False, Invalid(N.STEER, "`/steer` only works inside a session thread")),
    ("/digest", False, Command(N.DIGEST)),
    ("/digest", True, Invalid(N.DIGEST, "`/digest` only works outside a session thread")),
    ("/digest now", False, Invalid(N.DIGEST, "`/digest` takes no arguments", ("now",))),
    # away / back / misc
    ("/away", False, Command(N.AWAY)),
    ("/away on", True, Command(N.AWAY, ("on",))),
    ("/away OFF", False, Command(N.AWAY, ("off",))),
    ("/away maybe", False, Invalid(N.AWAY, "usage: `/away [on|off]`", ("maybe",))),
    ("/away on now", False, Invalid(N.AWAY, "usage: `/away [on|off]`", ("on", "now"))),
    ("/back", True, Command(N.BACK)),
    ("/back", False, Command(N.BACK)),
    ("/doctor", False, Command(N.DOCTOR)),
    ("/panic", True, Command(N.PANIC)),
    ("/panic now", False, Command(N.PANIC, ("now",))),
    ("/PANIC NOW", True, Command(N.PANIC, ("now",))),
    ("/panic later", False, Invalid(N.PANIC, "usage: `/panic [now]`", ("later",))),
    ("/help", False, Command(N.HELP)),
    ("/?", False, Command(N.HELP)),
    ("  /help  ", True, Command(N.HELP)),
    ("/frobnicate", True, Unknown("frobnicate")),
    ("/frobnicate", False, Unknown("frobnicate")),
]


@pytest.mark.parametrize(("text", "in_thread", "expected"), TABLE)
def test_parse(text: str, in_thread: bool, expected: Parsed) -> None:
    assert parse(text, in_thread) == expected


def test_every_command_appears_in_the_table() -> None:
    covered = {e.name for _, _, e in TABLE if isinstance(e, Command | Invalid)}
    assert covered == registry.names() == {n.value for n in N}


def test_unbalanced_quote_does_not_crash() -> None:
    assert parse('/new oc proj "unterminated', False) == Command(N.NEW, ("oc", "proj", '"unterminated'), None)


def test_help_is_generated_from_the_registry_and_lists_every_command() -> None:
    text = registry.help_text()
    for spec in registry.specs():
        assert f"`{spec.usage or '/' + spec.name}`" in text, spec.name
    assert {s.name for s in registry.specs()} == {n.value for n in N}
    assert text.splitlines()[0] == "**agent-relay commands**"
    assert [s.order for s in registry.specs()] == sorted(s.order for s in registry.specs())


def test_a_command_registered_in_its_own_module_parses_without_touching_the_parser() -> None:
    async def noop(*_: object) -> None:
        return None

    spec = registry.register(
        registry.CommandSpec(
            "ping", noop, "reply pong", aliases=("p",), scope="thread", usage="/ping [n]", max_args=1
        )
    )
    try:
        assert parse("/ping 3", True) == Command("ping", ("3",))
        assert parse("/p", True) == Command("ping")
        assert parse("/ping 1 2", True) == Invalid("ping", "usage: `/ping [n]`", ("1", "2"))
        assert parse("/ping", False) == Invalid("ping", "`/ping` only works inside a session thread")
        assert "`/ping [n]` reply pong (in a session thread)" in registry.help_text()
        with pytest.raises(ValueError, match="already registered"):
            registry.register(spec)
    finally:
        registry.unregister("ping")
    assert parse("/ping", True) == Unknown("ping")
    assert "/ping" not in registry.help_text()
