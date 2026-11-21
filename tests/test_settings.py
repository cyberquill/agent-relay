from __future__ import annotations

import logging
from pathlib import Path

import keyring.errors
import pytest

from agent_relay import paths
from agent_relay.models.config import RelayConfig, Workspace, WorkspacesConfig
from agent_relay.settings import (
    KeychainLockedError,
    MissingSecretError,
    Secret,
    code_workspace_folders,
    get_secret,
    legacy_config_keys,
    load_config,
    load_workspaces,
    lookup_secret,
    require_secret,
    resolve_workspace,
    save_config,
    save_workspaces,
    set_secret,
    unset_config_key,
    workspace_for_path,
)


def test_missing_secret_message_has_setup_command() -> None:
    with pytest.raises(MissingSecretError) as err:
        require_secret(Secret.WEBEX_BOT_TOKEN)
    assert "pbpaste | relay setup token --stdin" in str(err.value)


def test_secret_roundtrip() -> None:
    set_secret(Secret.PWA_TOKEN, "abc")
    assert require_secret(Secret.PWA_TOKEN) == "abc"


@pytest.mark.parametrize(
    "error",
    [
        keyring.errors.KeyringLocked("Can't get password from keychain: (-128, 'Keychain Access Denied')"),
        keyring.errors.KeyringError("Can't get password from keychain: (-25308, 'Unknown Error')"),
        keyring.errors.KeyringError("User interaction is not allowed."),
    ],
)
def test_a_locked_keychain_is_not_a_missing_secret(
    monkeypatch: pytest.MonkeyPatch, error: keyring.errors.KeyringError
) -> None:
    def locked(service: str, username: str) -> str:
        raise error

    monkeypatch.setattr(keyring, "get_password", locked)
    with pytest.raises(KeychainLockedError) as err:
        require_secret(Secret.WEBEX_BOT_TOKEN)
    assert type(err.value) is KeychainLockedError
    assert "locked" in str(err.value) and "unlock-keychain" in str(err.value)
    with pytest.raises(KeychainLockedError):
        lookup_secret(Secret.WEBEX_BOT_TOKEN)
    assert get_secret(Secret.WEBEX_BOT_TOKEN) is None


def test_another_keychain_failure_still_reads_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(service: str, username: str) -> str:
        raise keyring.errors.KeyringError("Can't get password from keychain: (-1, 'Unknown Error')")

    monkeypatch.setattr(keyring, "get_password", broken)
    with pytest.raises(MissingSecretError):
        require_secret(Secret.WEBEX_BOT_TOKEN)


def test_config_defaults_and_roundtrip() -> None:
    assert load_config() == RelayConfig()
    cfg = RelayConfig.model_validate({"webex": {"allowed_email": "me@example.com"}, "park": {"slice_s": 60}})
    saved = save_config(cfg)
    assert saved == paths.relay_toml()
    assert saved.stat().st_mode & 0o777 == 0o600
    assert load_config() == cfg
    assert cfg.park_hook_timeout_s() == 180
    assert cfg.question_hook_timeout_s() == cfg.questions.deadline_s + cfg.questions.hook_timeout_margin_s


def test_park_slice_validation() -> None:
    with pytest.raises(ValueError):
        RelayConfig.model_validate({"park": {"slice_s": 100, "deadline_s": 60}})


def test_workspaces(relay_home: Path) -> None:
    repo = relay_home / "repo"
    (repo / "sub").mkdir(parents=True)
    ws = WorkspacesConfig(workspaces=[Workspace(name="repo", path="~/repo")])
    save_workspaces(ws)
    loaded = load_workspaces()
    assert resolve_workspace("repo", loaded) == loaded.workspaces[0]
    assert resolve_workspace(str(repo), loaded) == loaded.workspaces[0]
    assert workspace_for_path(str(repo / "sub"), loaded) == loaded.workspaces[0]
    assert workspace_for_path("/elsewhere", loaded) is None


def test_code_workspace_folders_resolve_against_the_file(tmp_path: Path) -> None:
    file = tmp_path / "ws" / "team.code-workspace"
    file.parent.mkdir()
    file.write_text(
        '{\n  // comment\n  "folders": [\n'
        '    {"name": "a", "path": "../a"}, {"path": "/abs/b"}, {"name": "no-path"},\n  ],\n}'
    )
    assert code_workspace_folders(file) == [(tmp_path / "a").resolve(), Path("/abs/b").resolve()]


@pytest.mark.parametrize("body", ['{"folders": []}', '{"settings": {}}', "not json"])
def test_code_workspace_without_folders_is_rejected(tmp_path: Path, body: str) -> None:
    file = tmp_path / "x.code-workspace"
    file.write_text(body)
    with pytest.raises(ValueError):
        code_workspace_folders(file)


def test_workspace_file_roundtrips_and_is_omitted_when_unset() -> None:
    ws = WorkspacesConfig(
        workspaces=[
            Workspace(name="multi", path="/w/a", file="/w/m.code-workspace"),
            Workspace(name="one", path="/w/b"),
        ]
    )
    saved = save_workspaces(ws)
    assert load_workspaces() == ws
    assert saved.read_text().count("file") == 1


LEGACY_TOML = """
log_level = "DEBUG"

[webex]
allowed_email = "me@example.com"
bot_name = "agent-relay"
new_card_layout = "two"

[webex.bots]
vscode = "webex-bot-vscode"
cursor = "webex-bot-cursor"

[approvals]
deadline_s = 30
auto_allow = ["read"]
workspace_grant_s = 600

[presence]
auto_away_on_lock = false
poll_s = 5.0

[renderer]
flush_bytes = 2000
flush_idle_s = 5.0
progress_edit_interval_s = 10.0
quick_reply = true
max_message_bytes = 5000
"""

LEGACY_NAMES = [
    "approvals.auto_allow",
    "approvals.deadline_s",
    "approvals.workspace_grant_s",
    "presence.auto_away_on_lock",
    "presence.poll_s",
    "renderer.flush_bytes",
    "renderer.flush_idle_s",
    "renderer.progress_edit_interval_s",
    "renderer.quick_reply",
    "webex.bot_name",
    "webex.bots",
    "webex.new_card_layout",
]


def test_a_config_with_every_legacy_key_loads_with_one_warning(caplog: pytest.LogCaptureFixture) -> None:
    paths.relay_toml().parent.mkdir(parents=True, exist_ok=True)
    paths.relay_toml().write_text(LEGACY_TOML, encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        cfg = load_config()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "ignores" in r.getMessage()]
    assert len(warnings) == 1
    assert "webex.bots" in warnings[0].getMessage()
    assert cfg.webex.allowed_email == "me@example.com"
    assert cfg.log_level == "DEBUG"
    assert cfg.renderer.max_message_bytes == 5000
    assert cfg.renderer.html_threshold == "auto"
    assert cfg.questions.deadline_s == 600
    assert legacy_config_keys() == LEGACY_NAMES


def test_a_config_without_legacy_keys_reports_none_and_warns_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert legacy_config_keys() == []
    save_config(RelayConfig.model_validate({"webex": {"allowed_email": "me@example.com"}}))
    with caplog.at_level(logging.WARNING):
        load_config()
    assert not [r for r in caplog.records if "ignores" in r.getMessage()]
    assert legacy_config_keys() == []


def test_the_model_itself_tolerates_legacy_keys() -> None:
    cfg = RelayConfig.model_validate({"approvals": {"deadline_s": 30}, "webex": {"bots": {"cursor": "x"}}})
    assert cfg == RelayConfig()


def test_unknown_keys_are_still_rejected() -> None:
    with pytest.raises(ValueError):
        RelayConfig.model_validate({"webex": {"nonsense": 1}})
    with pytest.raises(ValueError):
        RelayConfig.model_validate({"renderer": {"html_threshold": "sometimes"}})


def test_unset_removes_a_legacy_key_and_writes_a_clean_file() -> None:
    paths.relay_toml().parent.mkdir(parents=True, exist_ok=True)
    paths.relay_toml().write_text(LEGACY_TOML, encoding="utf-8")
    unset_config_key("webex.bots")
    text = paths.relay_toml().read_text(encoding="utf-8")
    assert "bots" not in text and "approvals" not in text and "presence" not in text
    assert legacy_config_keys() == []
    assert load_config().webex.allowed_email == "me@example.com"


def test_unset_resets_a_current_key_and_refuses_an_absent_one() -> None:
    save_config(RelayConfig.model_validate({"park": {"slice_s": 60}}))
    unset_config_key("park.slice_s")
    assert load_config().park.slice_s == RelayConfig().park.slice_s
    with pytest.raises(ValueError):
        unset_config_key("park.nonexistent")
    with pytest.raises(ValueError):
        unset_config_key("nothing.here")
