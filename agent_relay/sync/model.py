"""The canonical MCP catalog stored in `~/.agents/mcp.yaml`.

Values may hold references instead of literals:
- `${env:NAME}`: an environment variable, translated to each harness's syntax.
- `${keychain:ACCOUNT}`: a secret in the Keychain (service `agent-relay-mcp`), written into native
  files only at render time, so `mcp.yaml` itself never holds a secret.
- Harness-only references (`${input:…}` for VS Code, `{file:…}` for OpenCode) are kept verbatim, and
  a server that uses one is rendered only for that harness.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class HarnessFile(StrEnum):
    VSCODE = "vscode"
    COPILOT = "copilot"
    CURSOR = "cursor"
    OPENCODE = "opencode"


HARNESS_ORDER: tuple[HarnessFile, ...] = tuple(HarnessFile)

Transport = Literal["stdio", "http", "sse"]

ENV_REF = re.compile(r"\$\{env:(?P<name>[A-Za-z_][A-Za-z0-9_]*)\}")
KEYCHAIN_REF = re.compile(r"\$\{keychain:(?P<account>[^}]+)\}")
PINNED_REFS: dict[HarnessFile, re.Pattern[str]] = {
    HarnessFile.VSCODE: re.compile(r"\$\{input:[^}]+\}"),
    HarnessFile.OPENCODE: re.compile(r"\{file:[^}]+\}"),
}


def strings(value: JsonValue) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)


class ServerDef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    transport: Transport
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_ms: int | None = None
    extras: dict[HarnessFile, dict[str, JsonValue]] = Field(default_factory=dict)

    def core(self) -> tuple[object, ...]:
        """What makes two definitions the same server; harness-specific extras do not count."""
        return (
            self.transport,
            self.command,
            tuple(self.args),
            tuple(sorted(self.env.items())),
            self.cwd,
            self.url,
            tuple(sorted(self.headers.items())),
        )

    def values(self) -> Iterator[str]:
        for single in (self.command, self.cwd, self.url):
            if single is not None:
                yield single
        yield from self.args
        yield from self.env.values()
        yield from self.headers.values()
        for extra in self.extras.values():
            yield from strings(extra)

    def pinned_to(self) -> HarnessFile | None:
        for harness, pattern in PINNED_REFS.items():
            if any(pattern.search(v) for v in self.values()):
                return harness
        return None

    def keychain_accounts(self) -> set[str]:
        return {m.group("account") for v in self.values() for m in KEYCHAIN_REF.finditer(v)}


class CatalogEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    server: ServerDef
    variants: dict[HarnessFile, ServerDef] = Field(default_factory=dict)
    sources: list[HarnessFile] = Field(default_factory=list)

    def for_harness(self, harness: HarnessFile) -> ServerDef:
        return self.variants.get(harness, self.server)


class Catalog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    servers: dict[str, CatalogEntry] = Field(default_factory=dict)

    def conflicts(self) -> dict[str, list[HarnessFile]]:
        """Names defined differently by some harnesses: each keeps its own variant."""
        return {name: sorted(e.variants) for name, e in self.servers.items() if e.variants}

    def for_harness(self, harness: HarnessFile) -> dict[str, ServerDef]:
        found: dict[str, ServerDef] = {}
        for name, entry in self.servers.items():
            server = entry.for_harness(harness)
            pinned = server.pinned_to()
            if pinned is None or pinned is harness:
                found[name] = server
        return found

    def skipped_for(self, harness: HarnessFile) -> dict[str, HarnessFile]:
        skipped: dict[str, HarnessFile] = {}
        for name, entry in self.servers.items():
            pinned = entry.for_harness(harness).pinned_to()
            if pinned is not None and pinned is not harness:
                skipped[name] = pinned
        return skipped
