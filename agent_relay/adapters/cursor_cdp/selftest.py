"""`relay selftest cursor-cdp`: every selector checked against the running Cursor, recorded per version.

The adapter refuses to drive a Cursor version that has no passing self-test on record.
"""

from __future__ import annotations

import json
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from agent_relay.adapters.base import AdapterUnavailableError
from agent_relay.adapters.cursor_cdp.connect import Connector, Launcher, connect, fetch_version
from agent_relay.adapters.cursor_cdp.driver import Composer
from agent_relay.adapters.cursor_cdp.selectors import SelectorSet
from agent_relay.core.registry import Registry
from agent_relay.models.events import utcnow

SELFTEST_KEY = "cursor_cdp.selftest"


class SelectorCheck(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    css: str
    required: bool
    matches: int

    @property
    def ok(self) -> bool:
        return self.matches > 0 or not self.required


class SelftestReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: str
    selectors_version: int
    checks: tuple[SelectorCheck, ...]
    at: datetime = Field(default_factory=utcnow)

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)

    def failures(self) -> list[str]:
        return [check.name for check in self.checks if not check.ok]

    def render(self) -> str:
        lines = [
            f"Cursor {self.version}, selectors v{self.selectors_version}: {'PASS' if self.ok else 'FAIL'}"
        ]
        for check in self.checks:
            mark = "ok " if check.matches else ("-- " if not check.required else "MISSING ")
            note = "" if check.required else " (optional)"
            lines.append(f"  {mark}{check.name}{note}: {check.matches} match(es) for {check.css}")
        return "\n".join(lines)


async def run_selftest(composer: Composer, selectors: SelectorSet, version: str) -> SelftestReport:
    checks = [
        SelectorCheck(name=name, css=sel.css, required=sel.required, matches=await composer.count(name))
        for name, sel in selectors.selectors.items()
    ]
    return SelftestReport(version=version, selectors_version=selectors.version, checks=tuple(checks))


async def run_live(
    registry: Registry,
    port: int,
    selectors: SelectorSet,
    launcher: Launcher | None = None,
    connector: Connector = connect,
) -> SelftestReport:
    """Starts Cursor if needed, checks the window showing the composer, and records the result."""
    version = await (launcher or Launcher()).ensure(port)
    return await _check(registry, port, version, selectors, connector)


async def check_attached(
    registry: Registry, port: int, selectors: SelectorSet, connector: Connector = connect
) -> SelftestReport | None:
    """Like `run_live`, but never starts Cursor: None when no debuggable Cursor is running."""
    version = await fetch_version(port)
    if version is None:
        return None
    return await _check(registry, port, version, selectors, connector)


async def _check(
    registry: Registry, port: int, version: str, selectors: SelectorSet, connector: Connector
) -> SelftestReport:
    conn = await connector(port)
    try:
        page = await conn.workbench(selectors, None)
        pages = conn.pages()
        target = page or (pages[0] if pages else None)
        if target is None:
            raise AdapterUnavailableError("Cursor exposes no window over CDP")
        report = await run_selftest(Composer(target, selectors), selectors, version)
    finally:
        await conn.close()
    await record(registry, report)
    return report


async def record(registry: Registry, report: SelftestReport) -> None:
    await registry.kv_set(SELFTEST_KEY, report.model_dump_json())


async def last_report(registry: Registry) -> SelftestReport | None:
    raw = await registry.kv_get(SELFTEST_KEY)
    if raw is None:
        return None
    try:
        return SelftestReport.model_validate(json.loads(raw))
    except ValueError:
        return None


def gate(report: SelftestReport | None, version: str, selectors: SelectorSet) -> str | None:
    """Why the adapter must stay disabled for this Cursor version, or None when it may run."""
    if report is None:
        return (
            "Cursor CDP has no self-test on record: run `relay selftest cursor-cdp` with Cursor's chat open."
        )
    if report.version != version:
        return (
            f"Cursor changed from {report.version} to {version}; the CDP adapter is disabled until "
            "`relay selftest cursor-cdp` passes again."
        )
    if report.selectors_version != selectors.version:
        return "The selectors changed since the last self-test: run `relay selftest cursor-cdp`."
    if not report.ok:
        return f"The last self-test failed ({', '.join(report.failures())}): fix selectors.json and rerun it."
    return None
