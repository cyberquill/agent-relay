from __future__ import annotations

from pathlib import Path

import pytest

from agent_relay import paths
from agent_relay.models.config import RelayConfig, Workspace, WorkspacesConfig
from agent_relay.settings import (
    MissingSecretError,
    Secret,
    load_config,
    load_workspaces,
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
