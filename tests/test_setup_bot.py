"""`relay setup bot` and the `webex.bots` config: tokens go in through stdin only and are never shown."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from agent_relay.cli import app
from agent_relay.models.config import CONTROL, RelayConfig, WebexConfig
from agent_relay.settings import (
    KEYCHAIN_SERVICE,
    BotItem,
    Secret,
    bot_account,
    bot_item,
    configured_bots,
    load_config,
    save_config,
    setup_command,
)
from tests.fakes.keyring_backend import MemoryKeyring

runner = CliRunner()
TOKEN = "fake-bot-token-0123456789"


def test_setup_bot_stores_the_token_from_stdin_and_never_prints_it(memory_keyring: MemoryKeyring) -> None:
    result = runner.invoke(app, ["setup", "bot", "vscode", "--stdin"], input=f"{TOKEN}\n")
    assert result.exit_code == 0, result.output
    assert memory_keyring.store[(KEYCHAIN_SERVICE, "webex-bot-vscode")] == TOKEN
    assert TOKEN not in result.output
    assert "relay config set webex.bots.vscode webex-bot-vscode" in result.output


def test_setup_bot_control_writes_the_original_item(memory_keyring: MemoryKeyring) -> None:
    result = runner.invoke(app, ["setup", "bot", "control", "--stdin"], input=TOKEN)
    assert result.exit_code == 0, result.output
    assert memory_keyring.store[(KEYCHAIN_SERVICE, Secret.WEBEX_BOT_TOKEN.value)] == TOKEN
    assert "webex.bots" not in result.output


def test_setup_bot_uses_the_configured_account(memory_keyring: MemoryKeyring) -> None:
    save_config(RelayConfig(webex=WebexConfig(bots={"cursor": "relay-cursor"})))
    result = runner.invoke(app, ["setup", "bot", "cursor", "--stdin"], input=TOKEN)
    assert result.exit_code == 0, result.output
    assert memory_keyring.store[(KEYCHAIN_SERVICE, "relay-cursor")] == TOKEN
    assert "relay config set" not in result.output


@pytest.mark.parametrize(
    ("args", "stdin"),
    [
        (["setup", "bot", "vscode"], TOKEN),
        (["setup", "bot", "vscode", "--stdin"], ""),
        (["setup", "bot", "vscode", "--stdin"], f"{TOKEN} {TOKEN}"),
        (["setup", "bot", "teams", "--stdin"], TOKEN),
    ],
)
def test_setup_bot_refuses_without_storing_or_echoing(
    memory_keyring: MemoryKeyring, args: list[str], stdin: str
) -> None:
    result = runner.invoke(app, args, input=stdin)
    assert result.exit_code != 0
    assert memory_keyring.store == {}
    assert TOKEN not in result.output


def test_config_set_adds_a_bot_and_rejects_shared_accounts() -> None:
    assert runner.invoke(app, ["config", "set", "webex.bots.vscode", "webex-bot-vscode"]).exit_code == 0
    assert load_config().webex.bots == {"vscode": "webex-bot-vscode"}
    for account in ("webex-bot-vscode", "pwa-token", "webex-bot-token", "two words"):
        result = runner.invoke(app, ["config", "set", "webex.bots.cursor", account])
        assert result.exit_code != 0, account
    assert load_config().webex.bots == {"vscode": "webex-bot-vscode"}


def test_bot_accounts_default_per_bot_and_control_stays_the_original_secret() -> None:
    webex = WebexConfig(bots={"opencode": "oc-bot"})
    assert bot_item(webex, CONTROL) is Secret.WEBEX_BOT_TOKEN
    assert bot_account(webex, "vscode") == "webex-bot-vscode"
    assert bot_item(webex, "opencode") == BotItem("opencode", "oc-bot")
    assert setup_command(bot_item(webex, "opencode")) == "pbpaste | relay setup bot opencode --stdin"


def test_configured_bots_always_include_control_in_a_fixed_order() -> None:
    assert configured_bots(WebexConfig()) == [CONTROL]
    webex = WebexConfig(bots={"opencode": "a", "vscode": "b"})
    assert configured_bots(webex) == [CONTROL, "vscode", "opencode"]
    assert configured_bots(WebexConfig(bots={"control": "my-control"})) == [CONTROL]
