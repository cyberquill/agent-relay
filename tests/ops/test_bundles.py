"""AC-33: the plugin bundles match the vendors' documented formats and start relay-mcp.

The schemas under tests/fixtures/schemas are transcribed from the vendor docs (each cites its URL).
Loader dry runs against the real Copilot CLI and OpenCode are in tests/live/test_bundle_loaders_live.py.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import jsonschema
import pytest
import yaml
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import JsonValue

from agent_relay import paths
from agent_relay.hook.installers import cursor
from agent_relay.hook.installers.opencode import PLUGIN_SOURCE
from agent_relay.models.config import RelayConfig
from agent_relay.ops import bundles

SCHEMAS = Path(__file__).resolve().parents[1] / "fixtures" / "schemas"


def schema(name: str) -> dict[str, JsonValue]:
    data: dict[str, JsonValue] = json.loads((SCHEMAS / f"{name}.schema.json").read_text(encoding="utf-8"))
    return data


def load(file: Path) -> dict[str, JsonValue]:
    data: dict[str, JsonValue] = json.loads(file.read_text(encoding="utf-8"))
    return data


def frontmatter(file: Path) -> dict[str, str]:
    found = bundles.FRONTMATTER.match(file.read_text(encoding="utf-8"))
    assert found is not None, f"{file} has no frontmatter"
    data = yaml.safe_load(found.group("head"))
    assert isinstance(data, dict)
    return {str(k): str(v) for k, v in data.items()}


@pytest.fixture
def built(tmp_path: Path) -> dict[str, Path]:
    return bundles.build_all(tmp_path / "dist", RelayConfig(), sys.executable)


def test_schemas_are_valid_json_schemas() -> None:
    for file in sorted(SCHEMAS.glob("*.schema.json")):
        jsonschema.Draft202012Validator.check_schema(load(file))


def test_copilot_bundle_matches_documented_format(built: dict[str, Path]) -> None:
    root = built["copilot"]
    manifest = load(root / "plugin.json")
    jsonschema.validate(manifest, schema("copilot-plugin"))
    for key in ("skills", "commands", "hooks", "mcpServers"):
        target = manifest[key]
        assert isinstance(target, str)
        assert (root / target).exists(), key
    jsonschema.validate(load(root / "hooks.json"), schema("copilot-hooks"))
    jsonschema.validate(load(root / ".mcp.json"), schema("mcp-servers"))


def test_cursor_bundle_matches_documented_format(built: dict[str, Path]) -> None:
    root = built["cursor"]
    jsonschema.validate(load(root / ".cursor-plugin" / "plugin.json"), schema("cursor-plugin"))
    hooks = load(root / "hooks" / "hooks.json")
    jsonschema.validate(hooks, schema("cursor-hooks"))
    assert isinstance(hooks["hooks"], dict)
    assert set(hooks["hooks"]) == set(cursor.EVENTS)
    jsonschema.validate(load(root / "mcp.json"), schema("mcp-servers"))
    assert sorted(p.name for p in (root / "commands").iterdir()) == ["away.md", "back.md"]


def test_opencode_bundle_is_a_config_overlay(built: dict[str, Path]) -> None:
    root = built["opencode"]
    assert (root / "plugins" / "agent-relay.ts").read_bytes() == PLUGIN_SOURCE.read_bytes()
    for name in bundles.COMMANDS:
        head = frontmatter(root / "command" / f"{name}.md")
        assert set(head) == {"description"}, "OpenCode commands take only a description"


def test_skill_and_command_frontmatter(built: dict[str, Path]) -> None:
    for root in built.values():
        for name in bundles.skill_names():
            head = frontmatter(root / "skills" / name / "SKILL.md")
            assert head["name"] == name
            assert 0 < len(head["description"]) <= 1024
    for harness in ("copilot", "cursor"):
        for name in bundles.COMMANDS:
            head = frontmatter(built[harness] / "commands" / f"{name}.md")
            assert head["name"] == name
            assert head["description"]


def test_every_skill_under_skills_ships_in_every_bundle(built: dict[str, Path]) -> None:
    names = bundles.skill_names()
    assert {"agent-relay-away", "agent-relay-recall"} <= set(names)
    for root in built.values():
        assert sorted(p.name for p in (root / "skills").iterdir()) == names
        for name in names:
            shipped = (root / "skills" / name / "SKILL.md").read_bytes()
            assert shipped == (bundles.SKILLS_DIR / name / "SKILL.md").read_bytes()


def test_skill_commands_are_not_copied_into_the_skill_folder(built: dict[str, Path]) -> None:
    for root in built.values():
        assert not (root / "skills" / bundles.SKILL_NAME / "commands").exists()


def test_the_skill_set_comes_from_the_folder_and_extra_files_ship(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    (skills / "one").mkdir(parents=True)
    (skills / "one" / "SKILL.md").write_text("---\nname: one\n---\n", encoding="utf-8")
    (skills / "one" / "notes.md").write_text("reference", encoding="utf-8")
    (skills / "two").mkdir()
    (skills / "two" / "SKILL.md").write_text("---\nname: two\n---\n", encoding="utf-8")
    assert bundles.skill_names(skills) == ["one", "two"]
    bundles._copy_skills(tmp_path / "out", skills)
    assert (tmp_path / "out" / "skills" / "one" / "notes.md").read_text(encoding="utf-8") == "reference"
    assert (tmp_path / "out" / "skills" / "two" / "SKILL.md").is_file()


def test_a_skill_folder_without_skill_md_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "broken").mkdir()
    with pytest.raises(ValueError, match="broken"):
        bundles.skill_names(tmp_path)


def test_recall_skill_is_read_only_and_names_every_store() -> None:
    text = (bundles.SKILLS_DIR / "agent-relay-recall" / "SKILL.md").read_text(encoding="utf-8")
    for fragment in (
        "chatSessions",
        "~/.copilot/session-state",
        "agent-transcripts",
        "cursorDiskKV",
        "opencode.db",
        "mode=ro",
        "not a continuation",
    ):
        assert fragment in text, fragment


def test_commands_flip_away_mode(built: dict[str, Path]) -> None:
    away = (built["cursor"] / "commands" / "away.md").read_text(encoding="utf-8")
    back = (built["cursor"] / "commands" / "back.md").read_text(encoding="utf-8")
    assert "relay away on --workspace" in away
    assert "relay away off --workspace" in back


def test_hooks_and_mcp_use_the_given_interpreter(tmp_path: Path) -> None:
    root = bundles.build_cursor(tmp_path, RelayConfig(), "/opt/py/bin/python3")
    hooks = json.dumps(load(root / "hooks" / "hooks.json"))
    assert "/opt/py/bin/python3 " in hooks
    servers = load(root / "mcp.json")["mcpServers"]
    assert isinstance(servers, dict)
    assert servers["agent-relay"] == {
        "command": "/opt/py/bin/python3",
        "args": ["-m", "agent_relay.mcp.server"],
    }


def test_rebuild_removes_stale_files(tmp_path: Path) -> None:
    root = bundles.build_opencode(tmp_path)
    (root / "stale.txt").write_text("old", encoding="utf-8")
    assert not (bundles.build_opencode(tmp_path) / "stale.txt").exists()


def test_bundle_paths_stay_inside_the_bundle(built: dict[str, Path]) -> None:
    for root in built.values():
        for file in root.rglob("*"):
            assert not file.is_symlink(), file
            assert file.resolve().is_relative_to(root.resolve())


@pytest.mark.parametrize("harness", ["copilot", "cursor"])
async def test_bundled_mcp_command_starts_relay_mcp(built: dict[str, Path], harness: str) -> None:
    file = ".mcp.json" if harness == "copilot" else "mcp.json"
    servers = load(built[harness] / file)["mcpServers"]
    assert isinstance(servers, dict)
    entry = servers["agent-relay"]
    assert isinstance(entry, dict)
    command, args = entry["command"], entry["args"]
    assert isinstance(command, str) and isinstance(args, list)
    env = {**os.environ, "RELAY_HOME": str(paths.home()), "RELAY_WORKSPACE": str(built[harness])}
    params = StdioServerParameters(
        command=command, args=[str(a) for a in args], env=env, cwd=str(bundles.REPO_ROOT)
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as client:
        await client.initialize()
        names = {tool.name for tool in (await client.list_tools()).tools}
    assert names == {"notify_phone", "ask_phone"}
