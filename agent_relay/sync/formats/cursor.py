"""Cursor `~/.cursor/mcp.json`: `{"mcpServers": {...}}`; the transport is implied by `command` or `url`."""

from __future__ import annotations

from pydantic import JsonValue

from agent_relay.sync.formats import Reader, Writer, as_object
from agent_relay.sync.model import HarnessFile, ServerDef
from agent_relay.sync.secrets import Revealer, SecretStash

HARNESS = HarnessFile.CURSOR
KEY = "mcpServers"
MAPPED = {"command", "args", "env", "cwd", "url", "headers"}


def read(document: JsonValue, stash: SecretStash) -> dict[str, ServerDef]:
    found: dict[str, ServerDef] = {}
    for name, raw in as_object(as_object(document).get(KEY)).items():
        entry = as_object(raw)
        r = Reader(HARNESS, name, stash)
        kind = str(entry.get("type") or ("http" if "url" in entry else "stdio"))
        extras = {k: r.tree(v, k, k) for k, v in entry.items() if k not in MAPPED}
        found[name] = ServerDef(
            transport="sse" if kind == "sse" else "http" if "url" in entry else "stdio",
            command=r.value(entry["command"], "command") if "command" in entry else None,
            args=r.strings(entry.get("args"), "args"),
            env=r.mapping(entry.get("env"), "env"),
            cwd=r.value(entry["cwd"], "cwd") if "cwd" in entry else None,
            url=r.value(entry["url"], "url") if "url" in entry else None,
            headers=r.mapping(entry.get("headers"), "headers"),
            extras={HARNESS: extras} if extras else {},
        )
    return found


def render(servers: dict[str, ServerDef], document: JsonValue, revealer: Revealer) -> dict[str, JsonValue]:
    w = Writer(HARNESS, revealer)
    rendered: dict[str, JsonValue] = {}
    for name, server in servers.items():
        entry: dict[str, JsonValue] = {}
        if server.transport == "stdio":
            entry["command"] = w.value(server.command or "")
            entry["args"] = w.strings(server.args)
            if server.env:
                entry["env"] = w.mapping(server.env)
            if server.cwd:
                entry["cwd"] = w.value(server.cwd)
        else:
            entry["url"] = w.value(server.url or "")
            if server.headers:
                entry["headers"] = w.mapping(server.headers)
        for key, value in server.extras.get(HARNESS, {}).items():
            entry[key] = w.tree(value)
        rendered[name] = entry
    out = as_object(document)
    out[KEY] = rendered
    return out
