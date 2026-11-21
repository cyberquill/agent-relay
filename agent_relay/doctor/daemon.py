"""Checks only relayd can answer: adapter health, the Webex link, away mode, self-tests, the CDP gate."""

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


class WebexStatus(Protocol):
    """What the doctor reads from `WebexHost`: `state` is starting, connected, rejected or unreachable."""

    @property
    def state(self) -> str: ...

    @property
    def last_error(self) -> str | None: ...

    @property
    def connections(self) -> int: ...


@dataclass
class DaemonView:
    service: RelayService
    registry: Registry
    config: RelayConfig
    webex: WebexStatus | None = None
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


TOKEN_FIX = "pbpaste | relay setup token --stdin"
TOKEN_REJECTED = "Webex token rejected (401)"


def webex_check(view: DaemonView) -> Check:
    host = view.webex
    name = "connection"
    if host is None:
        return Check(area="Webex", name=name, status="info", detail="not started in this process")
    state = host.state
    if state == "connected":
        return Check(area="Webex", name=name, status="ok", detail=f"connected ({host.connections} so far)")
    if state == "rejected":
        return Check(
            area="Webex",
            name=name,
            status="fail",
            detail=f"{host.last_error or TOKEN_REJECTED}; relayd is up and retries with backoff",
            fix=TOKEN_FIX,
        )
    if state == "unreachable":
        return Check(
            area="Webex",
            name=name,
            status="fail",
            detail=f"disconnected: {host.last_error or 'unknown error'}; relayd retries with backoff",
            fix="relayd reconnects on its own; check the network",
        )
    return Check(area="Webex", name=name, status="warn", detail="connecting")


def webex_checks(view: DaemonView) -> list[Check]:
    return [webex_check(view)]


async def update_checks(view: DaemonView) -> list[Check]:
    """The latest app-update self-test outcomes. They are no longer posted to the phone."""
    notices = await updates.latest_notices(view.registry)
    return [
        Check(
            area="Updates",
            name=harness,
            status="warn" if text.startswith("⚠️") else "ok",
            detail=f"{text} ({when[:16].replace('T', ' ')} UTC)",
            fix="relay selftest cursor-cdp" if harness == Harness.CURSOR_CDP.value else None,
        )
        for harness, (when, text) in sorted(notices.items())
    ]


async def away_checks(view: DaemonView) -> list[Check]:
    if paths.pause_flag().exists():
        return [
            Check(
                area="Away mode",
                name="state",
                status="warn",
                detail="paused by /panic: nothing is relayed",
                fix="send /away",
            )
        ]
    on = (await view.registry.away_scopes()).get(GLOBAL_SCOPE, False)
    checks = [
        Check(
            area="Away mode",
            name="state",
            status="ok",
            detail="on: desk chats come to the phone" if on else "off: send /away when you leave the desk",
        )
    ]
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
    checks += webex_checks(view)
    checks += await update_checks(view)
    checks += await away_checks(view)
    return checks
