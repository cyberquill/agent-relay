"""Build the catalog as the union of every harness's servers, and read/write `~/.agents/mcp.yaml`."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

import yaml
from pydantic import JsonValue

from agent_relay import paths
from agent_relay.sync import jsonc
from agent_relay.sync.formats import copilot, cursor, opencode, vscode
from agent_relay.sync.model import HARNESS_ORDER, Catalog, CatalogEntry, HarnessFile, ServerDef
from agent_relay.sync.secrets import SecretStash

RELAY_SERVER = "agent-relay"
RELAY_MCP_TIMEOUT_MS = 900_000

FORMATS: dict[HarnessFile, ModuleType] = {
    HarnessFile.VSCODE: vscode,
    HarnessFile.COPILOT: copilot,
    HarnessFile.CURSOR: cursor,
    HarnessFile.OPENCODE: opencode,
}


def native_path(harness: HarnessFile) -> Path:
    match harness:
        case HarnessFile.VSCODE:
            return paths.vscode_user_dir() / "mcp.json"
        case HarnessFile.COPILOT:
            return paths.copilot_dir() / "mcp-config.json"
        case HarnessFile.CURSOR:
            return paths.cursor_dir() / "mcp.json"
        case HarnessFile.OPENCODE:
            folder = paths.opencode_config_dir()
            plain = folder / "opencode.json"
            return (
                plain
                if plain.exists() and not (folder / "opencode.jsonc").exists()
                else folder / "opencode.jsonc"
            )


def read_document(harness: HarnessFile) -> JsonValue:
    file = native_path(harness)
    return jsonc.loads(file.read_text(encoding="utf-8")) if file.is_file() else {}


def read_servers(harness: HarnessFile, stash: SecretStash) -> dict[str, ServerDef]:
    servers: dict[str, ServerDef] = FORMATS[harness].read(read_document(harness), stash)
    return servers


def _merge(first: ServerDef, other: ServerDef) -> ServerDef:
    return first.model_copy(
        update={
            "extras": {**first.extras, **other.extras},
            "timeout_ms": first.timeout_ms if first.timeout_ms is not None else other.timeout_ms,
        }
    )


def union(found: dict[HarnessFile, dict[str, ServerDef]]) -> Catalog:
    """Every server from every harness. A name defined differently keeps each harness's variant."""
    entries: dict[str, CatalogEntry] = {}
    for harness in HARNESS_ORDER:
        for name, server in found.get(harness, {}).items():
            current = entries.get(name)
            if current is None:
                entries[name] = CatalogEntry(server=server, sources=[harness])
            elif server.core() == current.server.core():
                entries[name] = current.model_copy(
                    update={"server": _merge(current.server, server), "sources": [*current.sources, harness]}
                )
            else:
                entries[name] = current.model_copy(
                    update={
                        "variants": {**current.variants, harness: server},
                        "sources": [*current.sources, harness],
                    }
                )
    return Catalog(servers=entries)


def relay_server(python: str | None = None) -> ServerDef:
    return ServerDef(
        transport="stdio",
        command=python or sys.executable,
        args=["-m", "agent_relay.mcp.server"],
        timeout_ms=RELAY_MCP_TIMEOUT_MS,
    )


def with_relay(catalog: Catalog, python: str | None = None) -> Catalog:
    servers = dict(catalog.servers)
    servers[RELAY_SERVER] = CatalogEntry(server=relay_server(python))
    return catalog.model_copy(update={"servers": servers})


def import_all(stash: SecretStash, python: str | None = None) -> Catalog:
    return with_relay(union({h: read_servers(h, stash) for h in HARNESS_ORDER}), python)


def dump(catalog: Catalog) -> str:
    data = catalog.model_dump(mode="json", exclude_defaults=True)
    data["version"] = catalog.version
    header = "# Agent Relay MCP catalog. Rendered into every harness by `relay config sync`.\n"
    return header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def load(file: Path | None = None) -> Catalog:
    target = file or paths.mcp_yaml()
    if not target.is_file():
        return Catalog()
    data = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    return Catalog.model_validate(data)
