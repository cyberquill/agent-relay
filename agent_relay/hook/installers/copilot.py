"""`~/.copilot/hooks/agent-relay.json`: VS Code Local, the VS Code Copilot harness and Copilot CLI.

Shared events use PascalCase (VS Code-compatible snake_case payloads, which Local understands).
Copilot-only events use their documented camelCase names.
"""

from __future__ import annotations

from pydantic import JsonValue

from agent_relay import paths
from agent_relay.fileops import PlannedWrite, render_json
from agent_relay.hook.installers import hook_command, timeout_for
from agent_relay.models.config import RelayConfig

SHARED_EVENTS: dict[str, str] = {
    "SessionStart": "observe",
    "PreToolUse": "approval",
    "PostToolUse": "observe",
    "Stop": "park",
    "SubagentStop": "observe",
}
COPILOT_ONLY_EVENTS: dict[str, str] = {
    "permissionRequest": "approval",
    "notification": "observe",
    "sessionEnd": "observe",
    "errorOccurred": "observe",
}


def target() -> str:
    return str(paths.copilot_dir() / "hooks" / "agent-relay.json")


def render(config: RelayConfig, python: str | None = None) -> dict[str, JsonValue]:
    hooks: dict[str, JsonValue] = {}
    for event, kind in {**SHARED_EVENTS, **COPILOT_ONLY_EVENTS}.items():
        timeout = timeout_for(kind, config)
        hooks[event] = [
            {
                "type": "command",
                "command": hook_command("copilot", event, python),
                "timeoutSec": timeout,
                "timeout": timeout,
            }
        ]
    return {"version": 1, "hooks": hooks}


def plan(config: RelayConfig, python: str | None = None) -> list[PlannedWrite]:
    return [
        PlannedWrite(
            path=paths.copilot_dir() / "hooks" / "agent-relay.json",
            content=render_json(render(config, python)),
            reason="relay-hook for VS Code Local, the Copilot harness and Copilot CLI",
        )
    ]
