"""Install the OpenCode plugin file into `~/.config/opencode/plugins/`."""

from __future__ import annotations

from pathlib import Path

from agent_relay import paths
from agent_relay.fileops import PlannedWrite

REPO_ROOT = Path(__file__).resolve().parents[3]
PLUGIN_SOURCE = REPO_ROOT / "plugins" / "opencode" / "agent-relay.ts"


def target() -> Path:
    return paths.opencode_config_dir() / "plugins" / "agent-relay.ts"


def plan(source: Path | None = None) -> list[PlannedWrite]:
    src = source or PLUGIN_SOURCE
    return [PlannedWrite(path=target(), content=src.read_bytes(), reason="OpenCode relay plugin")]
