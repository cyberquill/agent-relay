"""Every home-relative path the relay uses. Stdlib-only: `relay-hook` imports it.

`RELAY_HOME` replaces the home directory, so tests and spikes never touch the real one.
"""

from __future__ import annotations

import os
from pathlib import Path

VSCODE_CLI = Path("/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code")
VSCODE_APP = Path("/Applications/Visual Studio Code.app")
CURSOR_APP = Path("/Applications/Cursor.app")
CURSOR_CLI_LAUNCHER = Path("/usr/local/bin/cursor")


def home() -> Path:
    override = os.environ.get("RELAY_HOME")
    return Path(override) if override else Path.home()


def config_dir() -> Path:
    return home() / ".config" / "agent-relay"


def relay_toml() -> Path:
    return config_dir() / "relay.toml"


def workspaces_toml() -> Path:
    return config_dir() / "workspaces.toml"


def app_support() -> Path:
    return home() / "Library" / "Application Support" / "agent-relay"


def db_path() -> Path:
    return app_support() / "relay.db"


def socket_path() -> Path:
    return app_support() / "relay.sock"


def lock_path() -> Path:
    return app_support() / "webex.lock"


def pause_flag() -> Path:
    return app_support() / "paused"


def versions_file() -> Path:
    return app_support() / "app-versions.json"


def install_manifest() -> Path:
    return app_support() / "install-manifest.json"


def backups_dir() -> Path:
    return app_support() / "backups"


def logs_dir() -> Path:
    return home() / "Library" / "Logs" / "agent-relay"


def launch_agents_dir() -> Path:
    return home() / "Library" / "LaunchAgents"


def copilot_dir() -> Path:
    return home() / ".copilot"


def cursor_dir() -> Path:
    return home() / ".cursor"


def cursor_cli_config() -> Path:
    return cursor_dir() / "cli-config.json"


def cursor_state_db() -> Path:
    return home() / "Library" / "Application Support" / "Cursor" / "User" / "globalStorage" / "state.vscdb"


def cursor_permissions_state() -> Path:
    return app_support() / "cursor-cli-permissions.json"


def claude_dir() -> Path:
    return home() / ".claude"


def opencode_config_dir() -> Path:
    return home() / ".config" / "opencode"


def agents_dir() -> Path:
    return home() / ".agents"


def agents_skills_dir() -> Path:
    return agents_dir() / "skills"


def mcp_yaml() -> Path:
    return agents_dir() / "mcp.yaml"


def skill_dup_backup_dir() -> Path:
    return agents_dir() / ".skill-dup-backup"


def vscode_user_dir() -> Path:
    return home() / "Library" / "Application Support" / "Code" / "User"


def local_bin() -> Path:
    return home() / ".local" / "bin"


def cursor_agent_cli() -> Path:
    return local_bin() / "cursor-agent"


def opencode_cli() -> Path:
    return home() / ".opencode" / "bin" / "opencode"


def expand_user(raw: str) -> Path:
    """Expand a leading `~` against `home()`, so config values honour `RELAY_HOME`."""
    if raw == "~":
        return home()
    if raw.startswith("~/"):
        return home() / raw[2:]
    return Path(raw)
