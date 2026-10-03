"""Render `~/.cursor/cli-config.json` permissions from the Cursor desktop allowlist.

The desktop keeps its auto-run allowlist in its state database (read here read-only). The relay
remembers which tokens it rendered last time, so a token removed on the desktop is removed from the
CLI too, while tokens the user added to the CLI by hand are kept.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from agent_relay import paths
from agent_relay.fileops import PlannedWrite, render_json

STATE_KEY = (
    "src.vs.platform.reactivestorage.browser.reactiveStorageServiceImpl.persistentStorage.applicationUser"
)


class IdeAllowlist(BaseModel):
    model_config = ConfigDict(frozen=True)

    commands: list[str] = Field(default_factory=list)
    denied: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)
    mcp_tools: list[str] = Field(default_factory=list)


class Rendered(BaseModel):
    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)


def _strings(raw: object) -> list[str]:
    return [s.strip() for s in raw if isinstance(s, str) and s.strip()] if isinstance(raw, list) else []


def read_ide_allowlist(db: Path | None = None) -> IdeAllowlist | None:
    target = db or paths.cursor_state_db()
    if not target.is_file():
        return None
    try:
        with sqlite3.connect(f"{target.resolve().as_uri()}?mode=ro", uri=True, timeout=2) as conn:
            row = conn.execute("SELECT value FROM ItemTable WHERE key = ?", (STATE_KEY,)).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return IdeAllowlist()
    try:
        state = json.loads(row[0]).get("composerState", {})
    except (json.JSONDecodeError, AttributeError):
        return None
    return IdeAllowlist(
        commands=_strings(state.get("yoloCommandAllowlist")),
        denied=_strings(state.get("yoloCommandDenylist")),
        domains=_strings(state.get("webFetchDomainAllowlist")),
        mcp_tools=_strings(state.get("mcpAllowedTools")),
    )


def shell_token(entry: str) -> str:
    """`git` → `Shell(git)`; `pip install` → `Shell(pip:install*)`, so a prefix never widens to the tool."""
    base, _, rest = entry.strip().partition(" ")
    return f"Shell({base})" if not rest.strip() else f"Shell({base}:{rest.strip()}*)"


def tokens(allowlist: IdeAllowlist) -> Rendered:
    allow = [shell_token(c) for c in allowlist.commands]
    allow += [f"WebFetch({d})" for d in allowlist.domains]
    allow += [f"Mcp({t})" for t in allowlist.mcp_tools if ":" in t]
    return Rendered(
        allow=list(dict.fromkeys(allow)), deny=list(dict.fromkeys(shell_token(c) for c in allowlist.denied))
    )


def _merge(existing: list[str], previous: list[str], fresh: list[str]) -> list[str]:
    kept = [t for t in existing if t not in previous or t in fresh]
    return kept + [t for t in fresh if t not in kept]


def plan(db: Path | None = None) -> tuple[list[PlannedWrite], str]:
    config = paths.cursor_cli_config()
    if not config.is_file():
        return [], f"Cursor CLI permissions: {config} does not exist (is the Cursor CLI installed?); skipped"
    allowlist = read_ide_allowlist(db)
    if allowlist is None:
        return [], "Cursor CLI permissions: the Cursor desktop allowlist could not be read; skipped"
    document = json.loads(config.read_text(encoding="utf-8") or "{}")
    state = paths.cursor_permissions_state()
    previous = (
        Rendered.model_validate_json(state.read_text(encoding="utf-8")) if state.is_file() else Rendered()
    )
    fresh = tokens(allowlist)
    permissions = document.get("permissions") if isinstance(document.get("permissions"), dict) else {}
    document["permissions"] = {
        **permissions,
        "allow": _merge(_strings(permissions.get("allow")), previous.allow, fresh.allow),
        "deny": _merge(_strings(permissions.get("deny")), previous.deny, fresh.deny),
    }
    writes = [
        PlannedWrite(
            path=config,
            content=render_json(document),
            mode=config.stat().st_mode & 0o777,
            reason="Cursor CLI permissions from the Cursor desktop allowlist",
        ),
        PlannedWrite(
            path=state, content=render_json(fresh.model_dump()), mode=0o600, reason="rendered tokens"
        ),
    ]
    counts = f"{len(fresh.allow)} allow and {len(fresh.deny)} deny token(s)"
    return writes, f"Cursor CLI permissions: {counts} from the desktop"
