"""AC-26 idempotent install, AC-27 byte-exact uninstall, AC-28 existing (git-ai) entries kept."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import JsonValue

from agent_relay import paths
from agent_relay.fileops import Manifest, PlannedWrite, apply_writes, restore
from agent_relay.hook.installers import copilot, cursor, opencode
from agent_relay.models.config import RelayConfig

_GIT_AI_CMD = "/Users/dev/.git-ai/bin/git-ai checkpoint cursor --hook-input stdin"
GIT_AI_HOOKS: dict[str, list[dict[str, JsonValue]]] = {
    event: [{"command": _GIT_AI_CMD}]
    for event in ("afterFileEdit", "beforeSubmitPrompt", "postToolUse", "preToolUse")
}
GIT_AI: dict[str, JsonValue] = {"version": 1, "hooks": {k: list(v) for k, v in GIT_AI_HOOKS.items()}}
# Deliberately unusual formatting: uninstall must restore bytes, not just equivalent JSON.
GIT_AI_BYTES = json.dumps(GIT_AI, indent=4).encode() + b"\n\n"


def _all_plans(config: RelayConfig) -> list[PlannedWrite]:
    return [*copilot.plan(config, "/py"), *cursor.plan(config, "/py"), *opencode.plan()]


def _seed() -> tuple[Path, Path]:
    hooks = cursor.hooks_file()
    hooks.parent.mkdir(parents=True)
    hooks.write_bytes(GIT_AI_BYTES)
    hooks.chmod(0o600)
    git_ai = paths.copilot_dir() / "hooks" / "git-ai.json"
    git_ai.parent.mkdir(parents=True)
    git_ai.write_text('{"version":1,"hooks":{"PreToolUse":[{"command":"git-ai"}]}}')
    return hooks, git_ai


def test_install_is_idempotent() -> None:
    _seed()
    config = RelayConfig()
    first = apply_writes(_all_plans(config), Manifest.load())
    assert len(first) == 3
    assert apply_writes(_all_plans(config), Manifest.load()) == []
    assert not any(p.changed() for p in _all_plans(config))


def test_git_ai_entries_are_kept_and_ours_added_once() -> None:
    hooks, git_ai = _seed()
    config = RelayConfig()
    apply_writes(_all_plans(config), Manifest.load())
    apply_writes(cursor.plan(config, "/other-python"), Manifest.load())
    merged = json.loads(hooks.read_text())
    for event, entries in GIT_AI_HOOKS.items():
        assert entries[0] in merged["hooks"][event]
    relay_entries = [e for e in merged["hooks"]["preToolUse"] if "relay_hook.py" in e["command"]]
    assert len(relay_entries) == 1 and relay_entries[0]["failClosed"] is True
    assert "/other-python" in relay_entries[0]["command"]
    assert git_ai.read_text().startswith('{"version":1')


def test_uninstall_restores_bytes_exactly_and_removes_created_files() -> None:
    hooks, _ = _seed()
    config = RelayConfig()
    apply_writes(_all_plans(config), Manifest.load())
    apply_writes(_all_plans(config), Manifest.load())
    restore(Manifest.load())
    assert hooks.read_bytes() == GIT_AI_BYTES
    assert hooks.stat().st_mode & 0o777 == 0o600
    assert not (paths.copilot_dir() / "hooks" / "agent-relay.json").exists()
    assert not opencode.target().exists()
    assert not opencode.target().parent.exists()
    assert Manifest.load().entries == {}


def test_cursor_install_into_empty_home() -> None:
    config = RelayConfig()
    apply_writes(cursor.plan(config, "/py"), Manifest.load())
    data = json.loads(cursor.hooks_file().read_text())
    assert set(data["hooks"]) == set(cursor.EVENTS)
    restore(Manifest.load())
    assert not cursor.hooks_file().exists()
    assert not paths.cursor_dir().exists()


def test_unmerge_drops_only_relay_entries() -> None:
    merged = cursor.merge(GIT_AI, RelayConfig(), "/py")
    assert cursor.unmerge(merged) == GIT_AI


def test_copilot_timeouts_follow_config() -> None:
    config = RelayConfig()
    hooks = copilot.render(config, "/py")["hooks"]
    assert isinstance(hooks, dict)
    pre = hooks["PreToolUse"]
    stop = hooks["Stop"]
    assert isinstance(pre, list) and isinstance(stop, list)
    entry = pre[0]
    assert isinstance(entry, dict)
    assert entry["timeoutSec"] == entry["timeout"] == config.approval_hook_timeout_s()
    assert isinstance(stop[0], dict) and stop[0]["timeoutSec"] == config.park_hook_timeout_s()


def test_diff_shows_planned_change() -> None:
    _seed()
    diff = cursor.plan(RelayConfig(), "/py")[0].diff()
    assert "+" in diff and "relay_hook.py" in diff
