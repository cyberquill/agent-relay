"""Checks only relayd can answer: live adapter health, the Webex link, away mode and the CDP gate."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from agent_relay import paths
from agent_relay.core import updates
from agent_relay.core.registry import GLOBAL_SCOPE, Registry
from agent_relay.core.service import RelayService
from agent_relay.core.updates import DEFAULT_APPS, WatchedApp, app_version
from agent_relay.doctor.model import Check
from agent_relay.models.config import RelayConfig
from agent_relay.models.events import Harness

ADAPTER_FIX: dict[Harness, str] = {
    Harness.VSCODE_COPILOT: "open VS Code with chat.agentHost.enabled on",
    Harness.VSCODE: "open VS Code with the relay-bridge extension installed",
    Harness.CURSOR_CLI: "run `cursor-agent login`",
    Harness.CURSOR_CDP: "relay selftest cursor-cdp",
    Harness.OPENCODE: "load the opencode launch agent (relay install --apply)",
}


class WebexLink(Protocol):
    """What the doctor reads from the websocket client."""

    connected: bool
    connections: int
    last_error: str | None


@dataclass
class DaemonView:
    service: RelayService
    registry: Registry
    config: RelayConfig
    webex: WebexLink | None = None
    apps: Sequence[WatchedApp] = DEFAULT_APPS


async def adapter_checks(view: DaemonView) -> list[Check]:
    checks: list[Check] = []
    for app in await view.service.apps():
        if app.healthy:
            checks.append(Check(area="Adapters", name=app.label, status="ok", detail=app.detail))
            continue
        degraded = await updates.degraded_reason(view.registry, app.harness)
        fix = "the relay retries the self-test every 10 minutes" if degraded else ADAPTER_FIX.get(app.harness)
        checks.append(Check(area="Adapters", name=app.label, status="fail", detail=app.detail, fix=fix))
    if not checks:
        checks.append(
            Check(
                area="Adapters",
                name="any",
                status="fail",
                detail="no adapter is enabled",
                fix="edit relay.toml",
            )
        )
    return checks


async def cdp_gate_check(view: DaemonView) -> list[Check]:
    if not view.config.cdp.enabled:
        return [Check(area="Adapters", name="Cursor CDP", status="info", detail="off (cdp.enabled = false)")]
    from agent_relay.adapters.cursor_cdp.selectors import load_selectors
    from agent_relay.adapters.cursor_cdp.selftest import gate, last_report

    cursor = next((app for app in view.apps if app.name == "Cursor"), None)
    version = app_version(cursor.plist) if cursor else None
    if version is None:
        return [
            Check(area="Adapters", name="Cursor CDP gate", status="info", detail="Cursor is not installed")
        ]
    report = await last_report(view.registry)
    reason = gate(report, version, load_selectors())
    if reason is not None:
        return [
            Check(
                area="Adapters",
                name="Cursor CDP gate",
                status="warn",
                detail=reason,
                fix="relay selftest cursor-cdp",
            )
        ]
    assert report is not None
    when = f"{report.at:%Y-%m-%d %H:%M}"
    detail = f"selectors v{report.selectors_version} passed on Cursor {report.version} at {when}"
    return [Check(area="Adapters", name="Cursor CDP gate", status="ok", detail=detail)]


def webex_check(view: DaemonView) -> Check:
    link = view.webex
    if link is None:
        return Check(area="Webex", name="websocket", status="info", detail="not started in this process")
    if link.connected:
        return Check(
            area="Webex", name="websocket", status="ok", detail=f"connected ({link.connections} so far)"
        )
    detail = f"disconnected: {link.last_error}" if link.last_error else "connecting"
    return Check(
        area="Webex",
        name="websocket",
        status="fail" if link.last_error else "warn",
        detail=detail,
        fix="relayd reconnects on its own; check the network and the bot token",
    )


async def away_checks(view: DaemonView) -> list[Check]:
    if paths.pause_flag().exists():
        return [
            Check(
                area="Away mode",
                name="state",
                status="warn",
                detail="paused by /panic: nothing is relayed",
                fix="send /away on",
            )
        ]
    scopes = await view.registry.away_scopes()
    on_global = scopes.get(GLOBAL_SCOPE, False)
    workspaces = sorted(scope for scope, enabled in scopes.items() if enabled and scope != GLOBAL_SCOPE)
    detail = "on everywhere" if on_global else "off globally"
    if workspaces:
        detail += f"; on for {', '.join(workspaces)}"
    checks = [Check(area="Away mode", name="state", status="ok", detail=detail)]
    pending = await view.registry.open_pending()
    if pending:
        checks.append(
            Check(
                area="Away mode", name="pending", status="info", detail=f"{len(pending)} request(s) waiting"
            )
        )
    return checks


async def daemon_checks(view: DaemonView) -> list[Check]:
    checks = await adapter_checks(view)
    checks += await cdp_gate_check(view)
    checks.append(webex_check(view))
    checks += await away_checks(view)
    return checks
