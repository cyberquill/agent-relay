"""Shared by the service parts: errors, app names, constants and the dependencies they all use."""

from __future__ import annotations

from dataclasses import dataclass

from agent_relay.core.lifecycle import Lifecycle
from agent_relay.core.registry import Registry
from agent_relay.core.router import Router
from agent_relay.core.signals import Signals
from agent_relay.models.capabilities import Capability
from agent_relay.models.config import RelayConfig
from agent_relay.models.events import Harness

APP_ALIASES: dict[str, Harness] = {
    "vscode": Harness.VSCODE,
    "copilot": Harness.VSCODE_COPILOT,
    "vscode-copilot": Harness.VSCODE_COPILOT,
    "ahp": Harness.VSCODE_COPILOT,
    "copilot-cli": Harness.COPILOT_CLI,
    "cursor": Harness.CURSOR_CLI,
    "cursor-cli": Harness.CURSOR_CLI,
    "cursor-ide": Harness.CURSOR_CDP,
    "cdp": Harness.CURSOR_CDP,
    "opencode": Harness.OPENCODE,
    "oc": Harness.OPENCODE,
}
APP_LABELS: dict[Harness, str] = {
    Harness.VSCODE: "VS Code",
    Harness.VSCODE_COPILOT: "VS Code · Copilot",
    Harness.COPILOT_CLI: "Copilot CLI",
    Harness.CURSOR: "Cursor IDE (hooks)",
    Harness.CURSOR_CLI: "Cursor CLI",
    Harness.CURSOR_CDP: "Cursor IDE (CDP)",
    Harness.OPENCODE: "OpenCode",
}
STICKY_MODEL_KEY = "model:{harness}"
DIFF_LIMIT_BYTES = 2 * 1024 * 1024
DESK_DECISION = "desk"


class RelayError(RuntimeError):
    """A user-facing failure: the message is shown as is."""


class DeliveryError(RelayError):
    """An answer could not reach the app that is waiting for it; the request is open again."""


@dataclass(frozen=True)
class AppInfo:
    harness: Harness
    label: str
    capabilities: tuple[Capability, ...]
    healthy: bool
    detail: str


@dataclass(frozen=True)
class Deps:
    registry: Registry
    router: Router
    signals: Signals
    config: RelayConfig
    lifecycle: Lifecycle


def resolve_app(name: str) -> Harness:
    lowered = name.strip().lower()
    if lowered in APP_ALIASES:
        return APP_ALIASES[lowered]
    try:
        return Harness(lowered)
    except ValueError as exc:
        raise RelayError(f"Unknown app `{name}`. Try `/apps`.") from exc
