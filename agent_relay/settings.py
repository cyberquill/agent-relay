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
from agent_relay.sync import jsonc

CODE_WORKSPACE_SUFFIX = ".code-workspace"

KEYCHAIN_SERVICE = "agent-relay"
KEYCHAIN_UNLOCK_COMMAND = "security unlock-keychain ~/Library/Keychains/login.keychain-db"


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


class KeychainLockedError(RuntimeError):
    """The Keychain refused to answer because it is locked or needs a user prompt."""

    def __init__(self, secret: Secret | None = None) -> None:
        self.secret = secret
        what = f"'{secret.value}'" if secret else "the relay's secrets"
        super().__init__(
            f"The login Keychain is locked, so {what} cannot be read. "
            f"Unlock it with: {KEYCHAIN_UNLOCK_COMMAND}"
        )


# errSecInteractionNotAllowed: the Keychain is locked and there is nobody to prompt (launchd, ssh).
_LOCKED_MARKERS = ("-25308", "user interaction is not allowed", "interaction not allowed")


def _is_locked(exc: keyring.errors.KeyringError) -> bool:
    if isinstance(exc, keyring.errors.KeyringLocked):
        return True
    text = str(exc).lower()
    return any(marker in text for marker in _LOCKED_MARKERS)


def lookup_secret(secret: Secret) -> str | None:
    """The stored value, None when absent or unreadable. Raises KeychainLockedError when locked."""
    try:
        value = keyring.get_password(KEYCHAIN_SERVICE, secret.value)
    except keyring.errors.KeyringError as exc:
        if _is_locked(exc):
            raise KeychainLockedError(secret) from exc
        return None
    return value or None


def get_secret(secret: Secret) -> str | None:
    """Like lookup_secret, but a locked Keychain reads as absent. Use lookup_secret to tell them apart."""
    try:
        return lookup_secret(secret)
    except KeychainLockedError:
        return None


def require_secret(secret: Secret) -> str:
    value = lookup_secret(secret)
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
    target.write_text(tomli_w.dumps(config.model_dump(mode="json", exclude_none=True)), encoding="utf-8")
    return target


def register_workspace(entry: Workspace) -> Path:
    """Append a workspace to workspaces.toml; a taken name is refused."""
    current = load_workspaces()
    if any(w.name == entry.name for w in current.workspaces):
        raise ValueError(f"a workspace named {entry.name!r} exists")
    return save_workspaces(WorkspacesConfig(workspaces=[*current.workspaces, entry]))


def replace_workspace(entry: Workspace) -> Path:
    """Swap the workspace of the same name for `entry`."""
    current = load_workspaces()
    if not any(w.name == entry.name for w in current.workspaces):
        raise ValueError(f"no workspace named {entry.name!r}")
    swapped = [entry if w.name == entry.name else w for w in current.workspaces]
    return save_workspaces(WorkspacesConfig(workspaces=swapped))


def code_workspace_folders(file: Path) -> list[Path]:
    """The folders of a VS Code `.code-workspace` file, resolved against the file's directory."""
    try:
        data = jsonc.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read {file}: {exc}") from exc
    entries = data.get("folders") if isinstance(data, dict) else None
    folders: list[Path] = []
    for entry in entries if isinstance(entries, list) else []:
        raw = entry.get("path") if isinstance(entry, dict) else None
        if isinstance(raw, str):
            folders.append((file.parent / paths.expand_user(raw)).resolve())
    if not folders:
        raise ValueError(f"{file} lists no folders")
    return folders


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
