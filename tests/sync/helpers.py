"""Install the MCP fixtures (today's file shapes, fake values) into the temporary RELAY_HOME."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from pydantic import JsonValue

from agent_relay.sync import catalog, jsonc
from agent_relay.sync.model import HarnessFile

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "mcp"
FIXTURE_FILES = {
    HarnessFile.VSCODE: "vscode-mcp.json",
    HarnessFile.COPILOT: "copilot-mcp-config.json",
    HarnessFile.CURSOR: "cursor-mcp.json",
    HarnessFile.OPENCODE: "opencode.jsonc",
}
FAKE_SECRETS = ("fake-sonar-value-0000", "fake-github-value-0000", "fake-client-value-0000")
SERVERS_KEY = {
    HarnessFile.VSCODE: "servers",
    HarnessFile.COPILOT: "mcpServers",
    HarnessFile.CURSOR: "mcpServers",
    HarnessFile.OPENCODE: "mcp",
}


def install_fixtures() -> None:
    for harness, name in FIXTURE_FILES.items():
        target = catalog.native_path(harness)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FIXTURES / name, target)


def fixture_names(harness: HarnessFile) -> set[str]:
    document = jsonc.loads((FIXTURES / FIXTURE_FILES[harness]).read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    servers = document.get(SERVERS_KEY[harness]) or {}
    assert isinstance(servers, dict)
    return set(servers)


def native_servers(harness: HarnessFile) -> dict[str, JsonValue]:
    """The harness file as strict JSON (what the renderers must produce)."""
    document = json.loads(catalog.native_path(harness).read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    servers = document.get(SERVERS_KEY[harness]) or {}
    assert isinstance(servers, dict)
    return servers
