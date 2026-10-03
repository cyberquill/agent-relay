"""Installable plugin bundles carrying relay-hook, relay-mcp and the away skill, one per harness.

- Copilot CLI: a plugin directory (`plugin.json` with skills, commands, hooks and MCP servers).
- Cursor: a Cursor plugin (`.cursor-plugin/plugin.json`), loadable from `~/.cursor/plugins/local`.
- OpenCode: a config-directory overlay (plugin file, commands and the skill).

Hook and MCP commands point at this checkout's interpreter, so a bundle is built for this Mac.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

from pydantic import JsonValue

from agent_relay import __version__
from agent_relay.hook.installers import copilot, cursor
from agent_relay.hook.installers.opencode import PLUGIN_SOURCE
from agent_relay.models.config import RelayConfig

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / "skills" / "agent-relay-away"
SKILL_NAME = "agent-relay-away"
COMMANDS = ("away", "back")
PLUGIN_NAME = "agent-relay"
DESCRIPTION = "Agent Relay: mirror coding-agent sessions to your phone over Webex, with /away and /back"
AUTHOR: dict[str, JsonValue] = {"name": "Agent Relay"}
FRONTMATTER = re.compile(r"\A---\n(?P<head>.*?)\n---\n", re.DOTALL)


def mcp_server(python: str | None = None) -> dict[str, JsonValue]:
    return {"command": python or sys.executable, "args": ["-m", "agent_relay.mcp.server"]}


def _write_json(target: Path, data: dict[str, JsonValue]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def _copy_skill(root: Path) -> None:
    target = root / "skills" / SKILL_NAME
    target.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SKILL_DIR / "SKILL.md", target / "SKILL.md")


def _command(name: str, keep_name: bool) -> str:
    """The command file; OpenCode's frontmatter takes only `description`, so `name` is dropped there."""
    text = (SKILL_DIR / "commands" / f"{name}.md").read_text(encoding="utf-8")
    if keep_name:
        return text
    found = FRONTMATTER.match(text)
    if found is None:
        return text
    head = "\n".join(line for line in found.group("head").splitlines() if not line.startswith("name:"))
    return f"---\n{head}\n---\n{text[found.end() :]}"


def _copy_commands(folder: Path, keep_name: bool = True) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for name in COMMANDS:
        (folder / f"{name}.md").write_text(_command(name, keep_name), encoding="utf-8")


def _fresh(target: Path) -> Path:
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    return target


def build_copilot(out: Path, config: RelayConfig, python: str | None = None) -> Path:
    root = _fresh(out / "copilot" / PLUGIN_NAME)
    manifest: dict[str, JsonValue] = {
        "name": PLUGIN_NAME,
        "version": __version__,
        "description": DESCRIPTION,
        "author": AUTHOR,
        "skills": "skills/",
        "commands": "commands/",
        "hooks": "hooks.json",
        "mcpServers": ".mcp.json",
    }
    _write_json(root / "plugin.json", manifest)
    _copy_skill(root)
    _copy_commands(root / "commands")
    _write_json(root / "hooks.json", copilot.render(config, python))
    server: dict[str, JsonValue] = {"type": "local", **mcp_server(python), "tools": ["*"]}
    _write_json(root / ".mcp.json", {"mcpServers": {PLUGIN_NAME: server}})
    return root


def build_cursor(out: Path, config: RelayConfig, python: str | None = None) -> Path:
    root = _fresh(out / "cursor" / PLUGIN_NAME)
    manifest: dict[str, JsonValue] = {
        "name": PLUGIN_NAME,
        "version": __version__,
        "description": DESCRIPTION,
        "author": AUTHOR,
    }
    _write_json(root / ".cursor-plugin" / "plugin.json", manifest)
    _copy_skill(root)
    _copy_commands(root / "commands")
    _write_json(root / "hooks" / "hooks.json", cursor.merge({}, config, python))
    _write_json(root / "mcp.json", {"mcpServers": {PLUGIN_NAME: mcp_server(python)}})
    return root


def build_opencode(out: Path) -> Path:
    root = _fresh(out / "opencode" / PLUGIN_NAME)
    (root / "plugins").mkdir()
    shutil.copyfile(PLUGIN_SOURCE, root / "plugins" / "agent-relay.ts")
    _copy_commands(root / "command", keep_name=False)
    _copy_skill(root)
    return root


def build_all(out: Path, config: RelayConfig, python: str | None = None) -> dict[str, Path]:
    return {
        "copilot": build_copilot(out, config, python),
        "cursor": build_cursor(out, config, python),
        "opencode": build_opencode(out),
    }
