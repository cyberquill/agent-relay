"""AC-8: a corpus of known secret shapes is fully redacted."""

from __future__ import annotations

import logging

import pytest

from agent_relay.log import get_logger
from agent_relay.scrub import REDACTED, Scrubbable, scrub_obj, scrub_text

_FAKE_WEBEX = "Y2lzY29zcGFyazovL3VzL1BFT1BMRS9hYmNkZWYxMjM0NTY3ODkwYWJjZGVmMTIzNDU2Nzg5MA_PF84_" + (
    "1eb65fdf-9643-417f-9974-ad72cae0e10f"
)

SECRETS = [
    "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8",
    "github_pat_" + "11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz",
    "AKIA" + "IOSFODNN7EXAMPLE",
    "AIza" + "SyA1234567890abcdefghijklmnopqrstuv",
    "sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc",
    "xoxb-" + "123456789012-abcdefghijkl",
    "sk-" + "proj-abcdefghijklmnopqrstuvwxyz012345",
    "eyJhbGciOiJIUzI1NiJ9." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    _FAKE_WEBEX,
    "4f9K2mQ7xR1tB8nV3cZ6wL0pY5hJ2gD9sA7eU4iO",
    "hf_" + "abcdefghijklmnopqrstuvwxyzabcdefgh",
]

CONTEXTS = [
    "token: {s}",
    "Authorization: Bearer {s}",
    '{{"connectionToken": "{s}"}}',
    "export WEBEX_TOKEN={s}",
    "the value is {s} ok",
    "ws://localhost/?tkn={s}&x=1",
]


@pytest.mark.parametrize("secret", SECRETS)
@pytest.mark.parametrize("context", CONTEXTS)
def test_corpus_redacted(secret: str, context: str) -> None:
    text = context.format(s=secret)
    out = scrub_text(text)
    assert secret not in out
    assert REDACTED in out


def test_key_value_and_url_credentials() -> None:
    out = scrub_text("password=hunter2hunter2 and https://user:s3cr3tpw@example.com/x")
    assert "hunter2hunter2" not in out
    assert "s3cr3tpw" not in out
    assert "https://user:" in out


def test_private_key_block() -> None:
    block = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nabc\n-----END RSA PRIVATE KEY-----"
    assert scrub_text(f"key:\n{block}\n") == f"key:\n{REDACTED}\n"


def test_ordinary_text_untouched() -> None:
    text = "Ran `uv run tox` in /Users/dev/Desktop/Repository/agent-relay at commit 5f2a9c1e0b7d4a3f8e6c"
    assert scrub_text(text) == text


def test_opencode_provider_keys_are_redacted() -> None:
    """OpenCode's `/config/providers` returns each provider's API key under a bare `key` field."""
    body = '{"id":"acme","source":"env","key":"plainlowercasekey","models":{}}'
    assert "plainlowercasekey" not in scrub_text(body)
    assert scrub_obj({"id": "acme", "key": "plainlowercasekey", "session_key": "s1"}) == {
        "id": "acme",
        "key": REDACTED,
        "session_key": "s1",
    }


def test_scrub_obj_redacts_secret_keys() -> None:
    obj: Scrubbable = {
        "connectionToken": "short",
        "nested": [{"password": "x"}, "Bearer abcdefghijkl"],
        "n": 1,
    }
    assert scrub_obj(obj) == {
        "connectionToken": REDACTED,
        "nested": [{"password": REDACTED}, REDACTED],
        "n": 1,
    }


def test_logger_scrubs(caplog: pytest.LogCaptureFixture) -> None:
    logger = get_logger("tests.scrub")
    with caplog.at_level(logging.INFO, logger="tests.scrub"):
        logger.info(f"bot token {SECRETS[0]}")
    assert SECRETS[0] not in caplog.text
    assert REDACTED in caplog.text
