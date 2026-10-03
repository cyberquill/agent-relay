"""OpenCode `opencode.jsonc`: `{"mcp": {name: {type: local|remote, ...}}}`, env references as `{env:NAME}`.

Local servers take `command` as one array and `environment` instead of `env`; `timeout` is in ms.
"""

from __future__ import annotations

from pydantic import JsonValue

from agent_relay.sync.formats import Reader, Writer, as_object
from agent_relay.sync.model import HarnessFile, ServerDef
from agent_relay.sync.secrets import Revealer, SecretStash

HARNESS = HarnessFile.OPENCODE
KEY = "mcp"
MAPPED = {"type", "command", "environment", "url", "headers", "timeout"}


def read(document: JsonValue, stash: SecretStash) -> dict[str, ServerDef]:
    found: dict[str, ServerDef] = {}
    for name, raw in as_object(as_object(document).get(KEY)).items():
        entry = as_object(raw)
        r = Reader(HARNESS, name, stash)
        argv = r.strings(entry.get("command"), "command")
        extras = {k: r.tree(v, k, k) for k, v in entry.items() if k not in MAPPED}
        timeout = entry.get("timeout")
        found[name] = ServerDef(
            transport="http" if entry.get("type") == "remote" else "stdio",
            command=argv[0] if argv else None,
            args=argv[1:],
            env=r.mapping(entry.get("environment"), "environment"),
            url=r.value(entry["url"], "url") if "url" in entry else None,
            headers=r.mapping(entry.get("headers"), "headers"),
            timeout_ms=timeout if isinstance(timeout, int) else None,
            extras={HARNESS: extras} if extras else {},
        )
    return found


def render(servers: dict[str, ServerDef], document: JsonValue, revealer: Revealer) -> dict[str, JsonValue]:
    w = Writer(HARNESS, revealer)
    rendered: dict[str, JsonValue] = {}
    for name, server in servers.items():
        entry: dict[str, JsonValue]
        if server.transport == "stdio":
            entry = {"type": "local", "command": w.strings([server.command or "", *server.args])}
            if server.env:
                entry["environment"] = w.mapping(server.env)
        else:
            entry = {"type": "remote", "url": w.value(server.url or "")}
            if server.headers:
                entry["headers"] = w.mapping(server.headers)
        if server.timeout_ms is not None:
            entry["timeout"] = server.timeout_ms
        for key, value in server.extras.get(HARNESS, {}).items():
            entry[key] = w.tree(value)
        rendered[name] = entry
    out = as_object(document)
    out[KEY] = rendered
    return out
