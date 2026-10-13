from __future__ import annotations

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
    load_config,
    load_workspaces,
    lookup_secret,
    require_secret,
    resolve_workspace,
    save_config,
    save_workspaces,
    set_secret,
    workspace_for_path,
)


def test_missing_secret_message_has_setup_command() -> None:
    with pytest.raises(MissingSecretError) as err:
        require_secret(Secret.WEBEX_BOT_TOKEN)
    assert "security add-generic-password -U -s agent-relay -a webex-bot-token -w" in str(err.value)


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
    assert cfg.approval_hook_timeout_s() > cfg.approvals.deadline_s


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
