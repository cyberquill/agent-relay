"""Readers and renderers for each harness's native MCP config, plus the shared value translation."""

from __future__ import annotations

import re

from pydantic import JsonValue

from agent_relay.sync.model import ENV_REF, HarnessFile
from agent_relay.sync.secrets import Revealer, SecretStash

_NATIVE_ENV: dict[HarnessFile, tuple[re.Pattern[str], str]] = {
    HarnessFile.VSCODE: (re.compile(r"\$\{env:(?P<name>[A-Za-z_][A-Za-z0-9_]*)\}"), "${{env:{name}}}"),
    HarnessFile.CURSOR: (re.compile(r"\$\{env:(?P<name>[A-Za-z_][A-Za-z0-9_]*)\}"), "${{env:{name}}}"),
    HarnessFile.COPILOT: (re.compile(r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)\}"), "${{{name}}}"),
    HarnessFile.OPENCODE: (re.compile(r"(?<!\$)\{env:(?P<name>[A-Za-z_][A-Za-z0-9_]*)\}"), "{{env:{name}}}"),
}


def canonical(value: str, harness: HarnessFile) -> str:
    pattern, _ = _NATIVE_ENV[harness]
    return pattern.sub(lambda m: f"${{env:{m.group('name')}}}", value)


def native(value: str, harness: HarnessFile, revealer: Revealer) -> str:
    _, template = _NATIVE_ENV[harness]
    translated = ENV_REF.sub(lambda m: template.format(name=m.group("name")), value)
    return revealer.reveal(translated)


class Reader:
    """Canonicalises one server's values and stashes literal secrets as it goes."""

    def __init__(self, harness: HarnessFile, server: str, stash: SecretStash) -> None:
        self.harness, self.server, self.stash = harness, server, stash

    def value(self, raw: JsonValue, location: str, key: str = "") -> str:
        text = canonical(str(raw), self.harness)
        return self.stash.keep(self.server, location, key or location, text)

    def mapping(self, raw: JsonValue, location: str) -> dict[str, str]:
        if not isinstance(raw, dict):
            return {}
        return {str(k): self.value(v, f"{location}.{k}", str(k)) for k, v in raw.items()}

    def strings(self, raw: JsonValue, location: str) -> list[str]:
        if not isinstance(raw, list):
            return []
        return [self.value(v, f"{location}.{i}", location) for i, v in enumerate(raw)]

    def tree(self, raw: JsonValue, location: str, key: str = "") -> JsonValue:
        if isinstance(raw, str):
            return self.value(raw, location, key)
        if isinstance(raw, list):
            return [self.tree(v, f"{location}.{i}", key) for i, v in enumerate(raw)]
        if isinstance(raw, dict):
            return {str(k): self.tree(v, f"{location}.{k}", str(k)) for k, v in raw.items()}
        return raw


class Writer:
    def __init__(self, harness: HarnessFile, revealer: Revealer) -> None:
        self.harness, self.revealer = harness, revealer

    def value(self, raw: str) -> str:
        return native(raw, self.harness, self.revealer)

    def mapping(self, raw: dict[str, str]) -> dict[str, JsonValue]:
        return {k: self.value(v) for k, v in raw.items()}

    def strings(self, raw: list[str]) -> list[JsonValue]:
        return [self.value(v) for v in raw]

    def tree(self, raw: JsonValue) -> JsonValue:
        if isinstance(raw, str):
            return self.value(raw)
        if isinstance(raw, list):
            return [self.tree(v) for v in raw]
        if isinstance(raw, dict):
            return {k: self.tree(v) for k, v in raw.items()}
        return raw


def as_object(document: JsonValue) -> dict[str, JsonValue]:
    return dict(document) if isinstance(document, dict) else {}
