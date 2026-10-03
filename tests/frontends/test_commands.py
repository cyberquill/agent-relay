"""AC-11: table tests for the command parser."""

from __future__ import annotations

import pytest

from agent_relay.frontends.commands import (
    Command,
    CommandName,
    HelpHint,
    Invalid,
    Parsed,
    Prompt,
    Unknown,
    parse,
)

N = CommandName

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
    assert covered == set(CommandName)


def test_unbalanced_quote_does_not_crash() -> None:
    assert parse('/new oc proj "unterminated', False) == Command(N.NEW, ("oc", "proj", '"unterminated'), None)
