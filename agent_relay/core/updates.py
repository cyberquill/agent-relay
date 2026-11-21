"""Update handling: when an app's `Info.plist` version changes, re-run its adapters' self-tests.

A failed self-test degrades the adapter (the AHP adapter is paused, the Cursor CDP adapter disabled):
phone actions on it are refused with the reason until a later self-test passes. Each outcome goes to the log
and to the registry, where `relay doctor` and `/doctor` read it; nothing is posted to the phone. A self-test
that cannot run yet (the app is closed) is retried on the next check.
"""

from __future__ import annotations

import asyncio
import json
import plistlib
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from agent_relay import paths
from agent_relay.core.registry import Registry
from agent_relay.log import get_logger
from agent_relay.models.events import Harness, utcnow

log = get_logger(__name__)

Outcome = Literal["pass", "fail", "unknown"]
type SelftestResult = tuple[Outcome, str]
type Selftest = Callable[[], Awaitable[SelftestResult]]

DEGRADED_KEY = "adapter.degraded.{harness}"
# The latest self-test outcome per harness, valued `<ISO-8601 UTC> <text>`; the doctor lists them.
NOTICE_PREFIX = "updates.notice:"
CHECK_EVERY_S = 600.0
DEGRADED_VERB: dict[Harness, str] = {Harness.VSCODE_COPILOT: "paused", Harness.CURSOR_CDP: "disabled"}
LABELS: dict[Harness, str] = {Harness.VSCODE_COPILOT: "VS Code agent host", Harness.CURSOR_CDP: "Cursor CDP"}


@dataclass(frozen=True)
class WatchedApp:
    name: str
    plist: Path
    harnesses: tuple[Harness, ...]


DEFAULT_APPS: tuple[WatchedApp, ...] = (
    WatchedApp("VS Code", paths.VSCODE_APP / "Contents" / "Info.plist", (Harness.VSCODE_COPILOT,)),
    WatchedApp("Cursor", paths.CURSOR_APP / "Contents" / "Info.plist", (Harness.CURSOR_CDP,)),
)


def app_version(plist: Path) -> str | None:
    try:
        with plist.open("rb") as fh:
            info = plistlib.load(fh)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    version = info.get("CFBundleShortVersionString") if isinstance(info, dict) else None
    return version if isinstance(version, str) and version else None


async def degraded_reason(registry: Registry, harness: Harness) -> str | None:
    return await registry.kv_get(DEGRADED_KEY.format(harness=harness.value))


async def latest_notices(registry: Registry) -> dict[str, tuple[str, str]]:
    """Harness value -> (when, text) of each recorded self-test outcome, from the registry."""
    found: dict[str, tuple[str, str]] = {}
    for key, value in (await registry.kv_prefix(NOTICE_PREFIX)).items():
        when, _, text = value.partition(" ")
        found[key[len(NOTICE_PREFIX) :]] = (when, text)
    return found


async def set_degraded(registry: Registry, harness: Harness, reason: str | None) -> None:
    key = DEGRADED_KEY.format(harness=harness.value)
    if reason is None:
        await registry.kv_delete(key)
    else:
        await registry.kv_set(key, reason)


@dataclass
class UpdateWatcher:
    registry: Registry
    selftests: dict[Harness, Selftest]
    apps: Sequence[WatchedApp] = DEFAULT_APPS
    versions_file: Path = field(default_factory=paths.versions_file)

    def _load(self) -> dict[str, str]:
        try:
            data = json.loads(self.versions_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}

    def _save(self, versions: dict[str, str]) -> None:
        self.versions_file.parent.mkdir(parents=True, exist_ok=True)
        self.versions_file.write_text(json.dumps(versions, indent=2, sort_keys=True), encoding="utf-8")

    async def _run(self, harness: Harness, why: str) -> tuple[Outcome, str | None]:
        """Run one self-test and update the degraded flag. Returns the outcome and a notice, if any."""
        try:
            outcome, detail = await self.selftests[harness]()
        except Exception as exc:
            outcome, detail = "fail", f"self-test crashed: {type(exc).__name__}"
        label = LABELS.get(harness, harness.value)
        was = await degraded_reason(self.registry, harness)
        if outcome == "fail":
            verb = DEGRADED_VERB.get(harness, "disabled")
            await set_degraded(self.registry, harness, f"{why}: the {label} self-test failed ({detail})")
            log.warning(f"{label} self-test failed after {why}; {verb}")
            text = f"⚠️ {why}: the {label} self-test failed ({detail})."
            return outcome, await self._record(harness, f"{text} It is {verb} until it passes.")
        if outcome == "pass":
            await set_degraded(self.registry, harness, None)
            if was is not None:
                done = f"✅ {label} passed its self-test again ({detail}); it is back on."
                return outcome, await self._record(harness, done)
            return outcome, await self._record(harness, f"✅ {why}: the {label} self-test passed ({detail}).")
        return outcome, None

    async def _record(self, harness: Harness, text: str) -> str:
        """Keep the outcome where the doctor reads it, and in the log. Nothing goes to the phone."""
        await self.registry.kv_set(f"{NOTICE_PREFIX}{harness.value}", f"{utcnow().isoformat()} {text}")
        log.info(text)
        return text

    async def check(self) -> list[str]:
        versions = self._load()
        notices: list[str] = []
        tested: set[Harness] = set()
        for app in self.apps:
            current = app_version(app.plist)
            if current is None:
                continue
            previous = versions.get(app.name)
            if previous is None:
                versions[app.name] = current
                continue
            if previous == current:
                continue
            settled = True
            for harness in app.harnesses:
                if harness not in self.selftests:
                    continue
                tested.add(harness)
                outcome, notice = await self._run(harness, f"{app.name} updated {previous} → {current}")
                settled = settled and outcome != "unknown"
                if notice:
                    notices.append(notice)
            if settled:
                versions[app.name] = current
        for harness in self.selftests:
            if harness not in tested and await degraded_reason(self.registry, harness) is not None:
                outcome, notice = await self._run(harness, "retry")
                if outcome == "pass" and notice:
                    notices.append(notice)
        self._save(versions)
        return notices

    async def run(self, every_s: float = CHECK_EVERY_S) -> None:
        while True:
            try:
                await self.check()
            except Exception:
                log.exception("update check failed")
            await asyncio.sleep(every_s)
