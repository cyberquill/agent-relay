"""AC-37 (daemon half): adapters, the CDP gate, the Webex host, away mode, self-tests, the IPC wiring."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from agent_relay import doctor, paths
from agent_relay.adapters.cursor_cdp.selectors import load_selectors
from agent_relay.adapters.cursor_cdp.selftest import SelectorCheck, SelftestReport, record
from agent_relay.core import away, updates
from agent_relay.core.registry import Registry
from agent_relay.core.router import Router
from agent_relay.core.service import RelayService
from agent_relay.core.signals import Signals
from agent_relay.core.updates import WatchedApp
from agent_relay.daemon import Core
from agent_relay.doctor.daemon import (
    DaemonView,
    adapter_checks,
    away_checks,
    cdp_gate_check,
    update_checks,
    webex_check,
)
from agent_relay.doctor.model import Check, Report
from agent_relay.models.config import CdpConfig, RelayConfig
from agent_relay.models.events import Harness
from tests.fakes.adapters import CrashingAdapter, EmittingAdapter
from tests.fakes.apps import write_plist


@dataclass
class Link:
    """Stands in for `WebexHost`: what the doctor reads."""

    state: str = "starting"
    connections: int = 0
    last_error: str | None = None


def view(
    registry: Registry, *adapters: object, config: RelayConfig | None = None, **kw: object
) -> DaemonView:
    router = Router()
    for adapter in adapters:
        router.add_adapter(adapter)  # type: ignore[arg-type]
    cfg = config or RelayConfig()
    return DaemonView(RelayService(registry, router, Signals(), cfg), registry, cfg, **kw)  # type: ignore[arg-type]


def cursor(tmp_path: Path, version: str = "3.23.12") -> Sequence[WatchedApp]:
    return (WatchedApp("Cursor", write_plist(tmp_path / "Cursor.app" / "Info.plist", version), ()),)


async def test_adapter_checks_report_health_degrade_and_absence(registry: Registry) -> None:
    healthy = await adapter_checks(view(registry, EmittingAdapter(count=0)))
    assert [(c.name, c.status) for c in healthy] == [("OpenCode", "ok")]
    await updates.set_degraded(registry, Harness.OPENCODE, "OpenCode updated 1 → 2")
    degraded = (await adapter_checks(view(registry, EmittingAdapter(count=0))))[0]
    assert degraded.status == "fail" and "OpenCode updated" in degraded.detail
    assert degraded.fix is not None and "retries" in degraded.fix
    crashing = (await adapter_checks(view(registry, CrashingAdapter())))[0]
    assert crashing.status == "fail" and crashing.fix
    assert (await adapter_checks(view(registry)))[0].detail == "no adapter is enabled"


async def test_cdp_gate_explains_why_it_is_closed_and_when_it_opens(
    registry: Registry, tmp_path: Path
) -> None:
    off = await cdp_gate_check(view(registry))
    assert off[0].status == "info"
    on = RelayConfig(cdp=CdpConfig(enabled=True))
    closed = (await cdp_gate_check(view(registry, config=on, apps=cursor(tmp_path))))[0]
    assert closed.status == "warn" and "no self-test on record" in closed.detail
    selectors = load_selectors()
    checks = tuple(
        SelectorCheck(name=name, css=sel.css, required=sel.required, matches=1)
        for name, sel in selectors.selectors.items()
    )
    await record(
        registry, SelftestReport(version="3.23.12", selectors_version=selectors.version, checks=checks)
    )
    opened = (await cdp_gate_check(view(registry, config=on, apps=cursor(tmp_path))))[0]
    assert opened.status == "ok" and "passed on Cursor 3.23.12" in opened.detail
    moved = (await cdp_gate_check(view(registry, config=on, apps=cursor(tmp_path / "new", "3.24.0"))))[0]
    assert moved.status == "warn" and "3.23.12 to 3.24.0" in moved.detail


def test_webex_check_tracks_the_host(registry: Registry) -> None:
    assert webex_check(view(registry)).status == "info"
    assert webex_check(view(registry, webex=Link("connected", connections=2))).status == "ok"
    assert webex_check(view(registry, webex=Link())).status == "warn"
    dropped = webex_check(
        view(registry, webex=Link("unreachable", last_error="ConnectionClosed: websocket dropped"))
    )
    assert dropped.status == "fail" and "ConnectionClosed" in dropped.detail
    assert dropped.fix is not None and "network" in dropped.fix


def test_doctor_reports_a_rejected_token_with_the_command_that_fixes_it(registry: Registry) -> None:
    rejected = webex_check(view(registry, webex=Link("rejected", last_error="Webex token rejected (401)")))
    assert rejected.status == "fail"
    assert "rejected (401)" in rejected.detail and "retries" in rejected.detail
    assert rejected.fix == "pbpaste | relay setup token --stdin"


async def test_away_checks_show_the_global_flag_and_panic(registry: Registry, tmp_path: Path) -> None:
    off = (await away_checks(view(registry)))[0]
    assert off.status == "ok" and off.detail.startswith("off") and "/away" in off.detail
    await away.set_away(registry, True, str(tmp_path))
    on = (await away_checks(view(registry)))[0]
    assert on.detail.startswith("on") and str(tmp_path) not in on.detail
    paths.pause_flag().parent.mkdir(parents=True, exist_ok=True)
    paths.pause_flag().touch()
    panic = (await away_checks(view(registry)))[0]
    assert panic.status == "warn" and panic.fix == "send /away"


async def test_update_checks_list_the_selftest_outcomes_that_are_no_longer_posted(
    registry: Registry,
) -> None:
    assert await update_checks(view(registry)) == []
    stamp = "2026-11-21T10:00:00+00:00"
    await registry.kv_set(f"{updates.NOTICE_PREFIX}vscode_copilot", f"{stamp} ✅ VS Code updated: passed")
    await registry.kv_set(f"{updates.NOTICE_PREFIX}cursor_cdp", f"{stamp} ⚠️ Cursor updated: failed")
    checks = {c.name: c for c in await update_checks(view(registry))}
    assert checks["vscode_copilot"].status == "ok" and "2026-11-21 10:00" in checks["vscode_copilot"].detail
    assert checks["cursor_cdp"].status == "warn"
    assert checks["cursor_cdp"].fix == "relay selftest cursor-cdp"


async def test_relayd_answers_the_doctor_op(registry: Registry) -> None:
    core = Core(RelayConfig(), registry)
    core.router.add_adapter(EmittingAdapter(count=0))
    core.webex = Link("connected", connections=1)
    reply = await core.handle_ipc({"op": "doctor"})
    raw = reply["checks"]
    assert isinstance(raw, list)
    names = {Check.model_validate(item).name for item in raw}
    assert {"OpenCode", "connection", "state"} <= names
    await core.router.stop()


async def test_doctor_without_relayd_still_runs_local_checks() -> None:
    daemon = await doctor.over_ipc()
    assert daemon[0].status == "fail" and daemon[0].fix == "relay start"

    async def runner(argv: Sequence[str]) -> tuple[int, str, str]:
        return 1, "", ""

    report = await doctor.run(RelayConfig(), "/usr/bin/python3", doctor.over_ipc, runner)
    assert not report.ok
    areas = {check.area for check in report.checks}
    assert {"relayd", "Versions", "Hooks", "Config", "Skills", "Keychain", "launchd"} <= areas


def test_report_renders_and_never_shows_secrets() -> None:
    leaked = "Bearer fake-github-value-0000-abcdef"
    report = Report(
        checks=[
            Check(area="A", name="one", status="ok", detail="fine"),
            Check(area="A", name="two", status="warn", detail=f"header {leaked}", fix="do x"),
        ]
    )
    text = report.render()
    assert "→ do x" in text and "fake-github-value" not in text
    data = json.loads(report.to_json())
    assert data["ok"] is True and data["counts"]["warn"] == 1
    assert "fake-github-value" not in report.to_json()
