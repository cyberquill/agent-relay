"""`relay setup token`: the token goes in through stdin only and is never shown."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from agent_relay.cli import app
from agent_relay.settings import KEYCHAIN_SERVICE, Secret, setup_command
from tests.fakes.keyring_backend import MemoryKeyring

runner = CliRunner()
TOKEN = "fake-bot-token-0123456789"


def test_setup_token_stores_the_token_from_stdin_and_never_prints_it(memory_keyring: MemoryKeyring) -> None:
    result = runner.invoke(app, ["setup", "token", "--stdin"], input=f"{TOKEN}\n")
    assert result.exit_code == 0, result.output
    assert memory_keyring.store[(KEYCHAIN_SERVICE, Secret.WEBEX_BOT_TOKEN.value)] == TOKEN
    assert TOKEN not in result.output


@pytest.mark.parametrize(
    ("args", "stdin"),
    [
        (["setup", "token"], TOKEN),
        (["setup", "token", "--stdin"], ""),
        (["setup", "token", "--stdin"], f"{TOKEN} {TOKEN}"),
    ],
)
def test_setup_token_refuses_without_storing_or_echoing(
    memory_keyring: MemoryKeyring, args: list[str], stdin: str
) -> None:
    result = runner.invoke(app, args, input=stdin)
    assert result.exit_code != 0
    assert memory_keyring.store == {}
    assert TOKEN not in result.output


def test_the_old_per_app_bot_command_is_gone(memory_keyring: MemoryKeyring) -> None:
    result = runner.invoke(app, ["setup", "bot", "vscode", "--stdin"], input=TOKEN)
    assert result.exit_code != 0
    assert memory_keyring.store == {}


def test_the_webex_token_setup_command_pipes_stdin() -> None:
    assert setup_command(Secret.WEBEX_BOT_TOKEN) == "pbpaste | relay setup token --stdin"
    assert "security add-generic-password" in setup_command(Secret.PWA_TOKEN)
