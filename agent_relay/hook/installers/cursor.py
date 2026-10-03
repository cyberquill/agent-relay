"""Merge relay-hook entries into `~/.cursor/hooks.json`, keeping every other entry (e.g. git-ai)."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import JsonValue

from agent_relay import paths
from agent_relay.fileops import PlannedWrite, render_json
from agent_relay.hook.installers import hook_command, is_relay_entry, timeout_for
from agent_relay.models.config import RelayConfig

EVENTS: dict[str, str] = {
    "sessionStart": "observe",
    "sessionEnd": "observe",
    "preToolUse": "approval",
    "postToolUse": "observe",
    "beforeShellExecution": "approval",
    "beforeMCPExecution": "approval",
    "afterShellExecution": "observe",
    "afterMCPExecution": "observe",
    "afterFileEdit": "observe",
    "afterAgentResponse": "observe",
    "afterAgentThought": "observe",
    "stop": "park",
}
FAIL_CLOSED = {"preToolUse", "beforeShellExecution", "beforeMCPExecution"}


def hooks_file() -> Path:
    return paths.cursor_dir() / "hooks.json"


def _entry(event: str, kind: str, config: RelayConfig, python: str | None) -> dict[str, JsonValue]:
    entry: dict[str, JsonValue] = {
        "command": hook_command("cursor", event, python),
        "timeout": timeout_for(kind, config),
    }
    if event in FAIL_CLOSED:
        entry["failClosed"] = True
    if event == "stop":
        entry["loop_limit"] = None
    return entry


def merge(
    existing: dict[str, JsonValue], config: RelayConfig, python: str | None = None
) -> dict[str, JsonValue]:
    hooks_raw = existing.get("hooks")
    hooks: dict[str, JsonValue] = dict(hooks_raw) if isinstance(hooks_raw, dict) else {}
    for event, kind in EVENTS.items():
        current = hooks.get(event)
        kept = [e for e in current if not is_relay_entry(e)] if isinstance(current, list) else []
        hooks[event] = [*kept, _entry(event, kind, config, python)]
    merged = dict(existing)
    merged["version"] = existing.get("version", 1)
    merged["hooks"] = dict(sorted(hooks.items()))
    return merged


def unmerge(existing: dict[str, JsonValue]) -> dict[str, JsonValue]:
    hooks_raw = existing.get("hooks")
    hooks: dict[str, JsonValue] = {}
    if isinstance(hooks_raw, dict):
        for event, entries in hooks_raw.items():
            if isinstance(entries, list):
                kept = [e for e in entries if not is_relay_entry(e)]
                if kept:
                    hooks[event] = kept
            else:
                hooks[event] = entries
    return {**existing, "hooks": hooks}


def read_existing() -> dict[str, JsonValue]:
    file = hooks_file()
    if not file.exists():
        return {"version": 1, "hooks": {}}
    data = json.loads(file.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {"version": 1, "hooks": {}}


def plan(config: RelayConfig, python: str | None = None) -> list[PlannedWrite]:
    return [
        PlannedWrite(
            path=hooks_file(),
            content=render_json(merge(read_existing(), config, python)),
            reason="relay-hook entries for the Cursor IDE (existing entries kept)",
        )
    ]
