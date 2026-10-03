"""Literal secrets in MCP configs move to the Keychain; `mcp.yaml` keeps only `${keychain:…}` references."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

import keyring
import keyring.errors

from agent_relay.scrub import REDACTED, scrub_text
from agent_relay.sync.model import ENV_REF, KEYCHAIN_REF, PINNED_REFS

MCP_KEYCHAIN_SERVICE = "agent-relay-mcp"
SECRET_NAME = re.compile(r"(token|secret|password|passwd|api[_-]?key|authorization|cookie|credential)", re.I)
_ACCOUNT_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")

type Resolver = Callable[[str], str | None]


class MissingMcpSecretError(RuntimeError):
    def __init__(self, account: str) -> None:
        self.account = account
        super().__init__(
            f"Keychain item '{account}' (service '{MCP_KEYCHAIN_SERVICE}') is missing; "
            "run `relay config import --apply` again"
        )


def is_reference(value: str) -> bool:
    if ENV_REF.fullmatch(value) or KEYCHAIN_REF.fullmatch(value):
        return True
    return any(p.fullmatch(value) for p in PINNED_REFS.values())


def is_secret(key: str, value: str) -> bool:
    if not value or is_reference(value):
        return False
    return bool(SECRET_NAME.search(key)) or scrub_text(value) != value


def account(server: str, location: str) -> str:
    return _ACCOUNT_CHARS.sub("_", f"{server}.{location}")


@dataclass
class SecretStash:
    """Secrets found while importing. Written to the Keychain only when the import is applied."""

    found: dict[str, str] = field(default_factory=dict)

    def keep(self, server: str, location: str, key: str, value: str) -> str:
        if not is_secret(key, value):
            return value
        name = account(server, location)
        self.found[name] = value
        return f"${{keychain:{name}}}"

    def store(self) -> list[str]:
        for name, value in self.found.items():
            keyring.set_password(MCP_KEYCHAIN_SERVICE, name, value)
        return sorted(self.found)


def keychain_resolver(name: str) -> str | None:
    try:
        return keyring.get_password(MCP_KEYCHAIN_SERVICE, name) or None
    except keyring.errors.KeyringError:
        return None


@dataclass
class Revealer:
    """Resolves `${keychain:…}` for native files and remembers each secret so output can hide it."""

    resolver: Resolver = keychain_resolver
    seen: set[str] = field(default_factory=set)

    def reveal(self, value: str) -> str:
        def swap(match: re.Match[str]) -> str:
            secret = self.resolver(match.group("account"))
            if secret is None:
                raise MissingMcpSecretError(match.group("account"))
            self.seen.add(secret)
            return secret

        return KEYCHAIN_REF.sub(swap, value)

    def hide(self, text: str) -> str:
        for secret in sorted(self.seen, key=len, reverse=True):
            text = text.replace(secret, REDACTED)
        return scrub_text(text)
