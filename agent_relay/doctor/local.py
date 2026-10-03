"""Checks that read this Mac directly: versions, installed files, Keychain and launchd.

None of them needs relayd; `relay doctor` runs them even when the daemon is down.
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path

from agent_relay import paths
from agent_relay.adapters.ahp.endpoints import run_cli
from agent_relay.core.updates import DEFAULT_APPS, WatchedApp, app_version
from agent_relay.doctor.model import Check, Status
from agent_relay.fileops import PlannedWrite
from agent_relay.hook.installers import HOOK_MARKER
from agent_relay.hook.installers import copilot as copilot_hooks
from agent_relay.hook.installers import cursor as cursor_hooks
from agent_relay.hook.installers import opencode as opencode_plugin
from agent_relay.models.config import RelayConfig
from agent_relay.ops import launchd, stepping_away
from agent_relay.ops.bundles import SKILL_NAME
from agent_relay.settings import SECRET_SETUP_COMMAND, Secret, get_secret
from agent_relay.sync.plan import plan_sync
from agent_relay.sync.secrets import MissingMcpSecretError, Resolver, keychain_resolver
from agent_relay.sync.skills import plan_dedupe

type Runner = Callable[[Sequence[str]], Awaitable[tuple[int, str, str]]]

SKEW_DAYS = 14
CLI_DATE = re.compile(r"(\d{4})\.(\d{1,2})\.(\d{1,2})")
CURSOR_UPDATE_FIX = "run `cursor-agent update` (the weekly launchd job does this on its own)"


async def cli_version(runner: Runner, binary: Path | str | None) -> str | None:
    if binary is None or not Path(binary).exists():
        return None
    try:
        rc, out, _ = await runner([str(binary), "--version"])
    except OSError:
        return None
    text = out.strip().splitlines()
    return text[0].strip() if rc == 0 and text else None


def cli_date(version: str) -> date | None:
    found = CLI_DATE.search(version)
    if found is None:
        return None
    try:
        return date(int(found[1]), int(found[2]), int(found[3]))
    except ValueError:
        return None


def app_updated(app: WatchedApp) -> date | None:
    try:
        return datetime.fromtimestamp(app.plist.stat().st_mtime, UTC).date()
    except OSError:
        return None


def skew(cli: str, app: WatchedApp) -> Check:
    built, updated = cli_date(cli), app_updated(app)
    name = "Cursor CLI vs desktop"
    if built is None or updated is None:
        return Check(area="Versions", name=name, status="info", detail=f"cannot compare {cli!r} with the app")
    lag = (updated - built).days
    if lag > SKEW_DAYS:
        return Check(
            area="Versions",
            name=name,
            status="warn",
            detail=f"the CLI ({built}) is {lag} days older than the desktop app (updated {updated})",
            fix=CURSOR_UPDATE_FIX,
        )
    return Check(area="Versions", name=name, status="ok", detail=f"CLI {built}, desktop updated {updated}")


async def version_checks(runner: Runner = run_cli, apps: Sequence[WatchedApp] = DEFAULT_APPS) -> list[Check]:
    checks: list[Check] = []
    cursor_app: WatchedApp | None = None
    for app in apps:
        version = app_version(app.plist)
        if app.name == "Cursor":
            cursor_app = app if version else None
        status = "ok" if version else "info"
        checks.append(Check(area="Versions", name=app.name, status=status, detail=version or "not installed"))
    copilot = shutil.which("copilot")
    clis: list[tuple[str, Path | str | None]] = [
        ("Cursor CLI", paths.cursor_agent_cli()),
        ("OpenCode", paths.opencode_cli()),
        ("Copilot CLI", copilot),
    ]
    cursor_cli: str | None = None
    for name, binary in clis:
        version = await cli_version(runner, binary)
        if name == "Cursor CLI":
            cursor_cli = version
        status = "ok" if version else "info"
        checks.append(Check(area="Versions", name=name, status=status, detail=version or "not installed"))
    if cursor_cli and cursor_app:
        checks.append(skew(cursor_cli, cursor_app))
    return checks


def _installed(write: PlannedWrite) -> bool:
    """A shared hooks file only counts once it carries a relay entry; the plugin is ours outright."""
    if not write.path.exists():
        return False
    return write.path.suffix == ".ts" or HOOK_MARKER.encode() in write.path.read_bytes()


def _freshness(name: str, writes: list[PlannedWrite]) -> Check:
    stale = [w for w in writes if w.changed()]
    if not stale:
        return Check(area="Hooks", name=name, status="ok", detail="installed and current")
    first = stale[0]
    detail = f"{first.path} is out of date" if _installed(first) else f"not installed in {first.path}"
    return Check(area="Hooks", name=name, status="warn", detail=detail, fix="relay install --apply")


def hook_checks(config: RelayConfig, python: str | None = None) -> list[Check]:
    return [
        _freshness("Copilot hooks", copilot_hooks.plan(config, python)),
        _freshness("Cursor hooks", cursor_hooks.plan(config, python)),
        _freshness("OpenCode plugin", opencode_plugin.plan()),
    ]


def mcp_checks(python: str | None = None, resolver: Resolver = keychain_resolver) -> list[Check]:
    name = "MCP servers"
    if not paths.mcp_yaml().exists():
        return [
            Check(
                area="Config",
                name=name,
                status="warn",
                detail=f"no {paths.mcp_yaml()}; each harness keeps its own list",
                fix="relay config import --apply",
            )
        ]
    try:
        planned = plan_sync(python=python, resolver=resolver)
    except MissingMcpSecretError as exc:
        return [
            Check(area="Config", name=name, status="fail", detail=str(exc), fix="relay config import --apply")
        ]
    drift = [w.path for w in planned.changed() if w.path != paths.cursor_permissions_state()]
    checks = [
        Check(
            area="Config",
            name=name,
            status="warn" if drift else "ok",
            detail=f"{len(drift)} file(s) differ from mcp.yaml: {', '.join(str(p) for p in drift)}"
            if drift
            else "every harness matches mcp.yaml",
            fix="relay config sync --apply" if drift else None,
        )
    ]
    checks += [Check(area="Config", name="MCP note", status="info", detail=note) for note in planned.notes]
    return checks


def skill_checks() -> list[Check]:
    checks: list[Check] = []
    dedupe = plan_dedupe()
    if dedupe.moves:
        names = sorted({move.source.name for move in dedupe.moves})
        checks.append(
            Check(
                area="Skills",
                name="Duplicates",
                status="warn",
                detail=f"{len(dedupe.moves)} harness copies duplicate ~/.agents/skills: {', '.join(names)}",
                fix="relay skills dedupe --apply",
            )
        )
    else:
        checks.append(
            Check(area="Skills", name="Duplicates", status="ok", detail="no duplicate skill folders")
        )
    checks += [
        Check(area="Skills", name=p.name, status="info", detail=f"{p} differs from ~/.agents/skills; kept")
        for p in dedupe.different
    ]
    away = paths.agents_skills_dir() / SKILL_NAME
    checks.append(
        Check(area="Skills", name=SKILL_NAME, status="ok", detail=f"linked at {away}")
        if away.exists()
        else Check(
            area="Skills",
            name=SKILL_NAME,
            status="warn",
            detail=f"missing at {away}",
            fix="relay install --apply",
        )
    )
    patch = stepping_away.state()
    status, detail = PATCH_STATUS[patch]
    fix = "relay install --apply" if patch is stepping_away.PatchState.PENDING else None
    checks.append(Check(area="Skills", name="stepping-away", status=status, detail=detail, fix=fix))
    return checks


PATCH_STATUS: dict[stepping_away.PatchState, tuple[Status, str]] = {
    stepping_away.PatchState.APPLIED: ("ok", "patched; it no longer posts to Webex itself"),
    stepping_away.PatchState.PENDING: ("warn", "unpatched; it would post to Webex alongside the relay"),
    stepping_away.PatchState.DRIFTED: ("warn", "edited since the patch was written; left alone"),
    stepping_away.PatchState.MISSING: ("info", "not installed"),
}


def keychain_checks(config: RelayConfig) -> list[Check]:
    wanted: list[tuple[Secret, Status, str]] = [(Secret.WEBEX_BOT_TOKEN, "fail", "relayd cannot reach Webex")]
    if config.adapters.opencode:
        wanted.append(
            (Secret.OPENCODE_PASSWORD, "warn", "the OpenCode launch agent will not start the server")
        )
    if config.pwa.enabled:
        wanted.append((Secret.PWA_TOKEN, "fail", "the PWA cannot authenticate the phone"))
    checks: list[Check] = []
    for secret, missing, why in wanted:
        present = get_secret(secret) is not None
        checks.append(
            Check(area="Keychain", name=secret.value, status="ok", detail="present")
            if present
            else Check(
                area="Keychain",
                name=secret.value,
                status=missing,
                detail=f"missing; {why}",
                fix=SECRET_SETUP_COMMAND[secret],
            )
        )
    return checks


async def launchd_checks(python: str, runner: Runner = run_cli) -> list[Check]:
    checks: list[Check] = []
    domain = f"gui/{os.getuid()}"
    for agent in launchd.agents(python):
        try:
            rc, _, _ = await runner(["launchctl", "print", f"{domain}/{agent.label}"])
        except OSError:
            rc = 1
        installed = agent.path.exists()
        if rc == 0:
            checks.append(Check(area="launchd", name=agent.name, status="ok", detail=f"{agent.label} loaded"))
        else:
            checks.append(
                Check(
                    area="launchd",
                    name=agent.name,
                    status="warn",
                    detail=f"{agent.label} is {'installed but not loaded' if installed else 'not installed'}",
                    fix="relay install --apply"
                    if not installed
                    else f"launchctl bootstrap {domain} {agent.path}",
                )
            )
    return checks
