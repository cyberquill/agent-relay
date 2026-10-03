"""Hook installers. Each returns `PlannedWrite`s; `relay install` shows or applies them."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

from agent_relay.hook import relay_hook
from agent_relay.models.config import RelayConfig

HOOK_MARKER = "relay_hook.py"


def hook_command(family: str, event: str, python: str | None = None, script: Path | None = None) -> str:
    interpreter = python or sys.executable
    target = script or Path(relay_hook.__file__).resolve()
    return (
        f"{shlex.quote(interpreter)} -S -E {shlex.quote(str(target))} "
        f"--harness {shlex.quote(family)} --event {shlex.quote(event)}"
    )


def is_relay_entry(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    return any(isinstance(v, str) and HOOK_MARKER in v for v in entry.values())


def timeout_for(kind: str, config: RelayConfig) -> int:
    if kind == "approval":
        return config.approval_hook_timeout_s()
    if kind == "park":
        return config.park_hook_timeout_s()
    return 10
