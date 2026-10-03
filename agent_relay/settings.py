"""Configuration files and Keychain secrets."""

from __future__ import annotations

import tomllib
from enum import StrEnum
from pathlib import Path

import keyring
import keyring.errors
import tomli_w

from agent_relay import paths
from agent_relay.models.config import RelayConfig, Workspace, WorkspacesConfig

KEYCHAIN_SERVICE = "agent-relay"


class Secret(StrEnum):
    WEBEX_BOT_TOKEN = "webex-bot-token"
    PWA_TOKEN = "pwa-token"
    OPENCODE_PASSWORD = "opencode-password"


SECRET_SETUP_COMMAND: dict[Secret, str] = {
    secret: f"security add-generic-password -U -s {KEYCHAIN_SERVICE} -a {secret.value} -w"
    for secret in Secret
}


class MissingSecretError(RuntimeError):
    def __init__(self, secret: Secret) -> None:
        self.secret = secret
        super().__init__(
            f"Keychain item '{secret.value}' (service '{KEYCHAIN_SERVICE}') is missing. "
            f"Add it with: {SECRET_SETUP_COMMAND[secret]}"
        )


def get_secret(secret: Secret) -> str | None:
    try:
        value = keyring.get_password(KEYCHAIN_SERVICE, secret.value)
    except keyring.errors.KeyringError:
        return None
    return value or None


def require_secret(secret: Secret) -> str:
    value = get_secret(secret)
    if value is None:
        raise MissingSecretError(secret)
    return value


def set_secret(secret: Secret, value: str) -> None:
    keyring.set_password(KEYCHAIN_SERVICE, secret.value, value)


def load_config(file: Path | None = None) -> RelayConfig:
    target = file or paths.relay_toml()
    if not target.exists():
        return RelayConfig()
    with target.open("rb") as fh:
        return RelayConfig.model_validate(tomllib.load(fh))


def save_config(config: RelayConfig, file: Path | None = None) -> Path:
    target = file or paths.relay_toml()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(tomli_w.dumps(config.model_dump(mode="json")), encoding="utf-8")
    target.chmod(0o600)
    return target


def load_workspaces(file: Path | None = None) -> WorkspacesConfig:
    target = file or paths.workspaces_toml()
    if not target.exists():
        return WorkspacesConfig()
    with target.open("rb") as fh:
        return WorkspacesConfig.model_validate(tomllib.load(fh))


def save_workspaces(config: WorkspacesConfig, file: Path | None = None) -> Path:
    target = file or paths.workspaces_toml()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(tomli_w.dumps(config.model_dump(mode="json")), encoding="utf-8")
    return target


def resolve_workspace(name_or_path: str, workspaces: WorkspacesConfig) -> Workspace | None:
    for ws in workspaces.workspaces:
        if ws.name == name_or_path:
            return ws
    candidate = paths.expand_user(name_or_path).resolve()
    for ws in workspaces.workspaces:
        if paths.expand_user(ws.path).resolve() == candidate:
            return ws
    return None


def workspace_for_path(cwd: str, workspaces: WorkspacesConfig) -> Workspace | None:
    """The configured workspace that contains `cwd`, preferring the deepest match."""
    target = paths.expand_user(cwd).resolve()
    best: Workspace | None = None
    best_depth = -1
    for ws in workspaces.workspaces:
        root = paths.expand_user(ws.path).resolve()
        if target == root or root in target.parents:
            depth = len(root.parts)
            if depth > best_depth:
                best, best_depth = ws, depth
    return best
