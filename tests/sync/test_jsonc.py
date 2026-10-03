"""JSONC parsing for VS Code `mcp.json` and `opencode.jsonc`."""

from __future__ import annotations

import pytest

from agent_relay.sync import jsonc


def test_line_and_block_comments_are_removed() -> None:
    text = '{\n // note\n "a": 1, /* block\n comment */ "b": [1, 2]\n}'
    assert jsonc.loads(text) == {"a": 1, "b": [1, 2]}


def test_comment_markers_inside_strings_survive() -> None:
    text = '{"url": "https://example.com/a//b", "glob": "/* not a comment */"}'
    assert jsonc.loads(text) == {"url": "https://example.com/a//b", "glob": "/* not a comment */"}


def test_trailing_commas_are_dropped_outside_strings() -> None:
    text = '{"a": [1, 2,], "s": "x,]", "o": {"k": "v",},}'
    assert jsonc.loads(text) == {"a": [1, 2], "s": "x,]", "o": {"k": "v"}}


def test_escaped_quotes_do_not_end_strings() -> None:
    assert jsonc.loads('{"q": "say \\"hi\\" // ok"}') == {"q": 'say "hi" // ok'}


def test_empty_text_is_an_empty_object() -> None:
    assert jsonc.loads("  \n") == {}


def test_unterminated_block_comment_is_an_error() -> None:
    with pytest.raises(ValueError, match="unterminated"):
        jsonc.loads('{"a": 1 /* oops')


def test_dumps_is_strict_json() -> None:
    assert jsonc.dumps({"a": "é"}) == '{\n  "a": "é"\n}\n'
