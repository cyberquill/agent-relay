"""AC-37 (local half): versions and skew, hooks, MCP and skills drift, Keychain and launchd."""

from __future__ import annotations

import os
import shutil
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import httpx
import keyring
import keyring.errors
import pytest

from agent_relay import doctor, paths
from agent_relay.core.updates import WatchedApp
from agent_relay.doctor import local
from agent_relay.doctor.model import Check
from agent_relay.fileops import Manifest, apply_writes
from agent_relay.hook.installers import copilot as copilot_hooks
from agent_relay.hook.installers import cursor as cursor_hooks
from agent_relay.hook.installers import opencode as opencode_plugin
from agent_relay.models.config import PwaConfig, QuestionsConfig, RelayConfig
from agent_relay.ops import stepping_away
from agent_relay.settings import KEYCHAIN_SERVICE, Secret
from agent_relay.sync.plan import plan_import, plan_sync
from agent_relay.sync.secrets import MCP_KEYCHAIN_SERVICE
from tests.fakes.apps import write_plist
from tests.fakes.keyring_backend import MemoryKeyring
from tests.sync.helpers import install_fixtures

PYTHON = "/usr/bin/python3"


class Runner:
    """Answers `<binary> --version` and `launchctl print` from a table; records every call."""

    def __init__(self, replies: dict[str, tuple[int, str]]) -> None:
        self.replies = replies
        self.calls: list[list[str]] = []

    async def __call__(self, argv: Sequence[str]) -> tuple[int, str, str]:
        self.calls.append(list(argv))
        for key, (rc, out) in self.replies.items():
            if key in " ".join(argv):
                return rc, out, ""
        return 1, "", "no such thing"


def by_name(checks: list[Check]) -> dict[str, Check]:
    return {check.name: check for check in checks}


def fake_binary(target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    target.chmod(0o755)
    return target


def cursor_app(tmp_path: Path, updated: datetime) -> WatchedApp:
    plist = write_plist(tmp_path / "Cursor.app" / "Contents" / "Info.plist", "3.23.12")
    os.utime(plist, (updated.timestamp(), updated.timestamp()))
    return WatchedApp("Cursor", plist, ())


@pytest.fixture(autouse=True)
def no_copilot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: None)


async def test_versions_report_apps_and_clis(tmp_path: Path) -> None:
    fake_binary(paths.cursor_agent_cli())
    runner = Runner({"cursor-agent --version": (0, "2026.09.20-0a1b2c3\n")})
    apps = [cursor_app(tmp_path, datetime(2026, 9, 25)), WatchedApp("VS Code", tmp_path / "none.plist", ())]
    checks = by_name(await local.version_checks(runner, apps))
    assert checks["Cursor"].detail == "3.23.12"
    assert checks["VS Code"].status == "info"
    assert checks["Cursor CLI"].detail == "2026.09.20-0a1b2c3"
    assert checks["OpenCode"].detail == "not installed"
    assert checks["Cursor CLI vs desktop"].status == "ok"


async def test_an_old_cursor_cli_is_flagged_as_skew(tmp_path: Path) -> None:
    fake_binary(paths.cursor_agent_cli())
    runner = Runner({"cursor-agent --version": (0, "2026.07.17-deadbee")})
    checks = by_name(await local.version_checks(runner, [cursor_app(tmp_path, datetime(2026, 9, 30))]))
    skew = checks["Cursor CLI vs desktop"]
    assert skew.status == "warn"
    assert "days older than the desktop app" in skew.detail
    assert skew.fix is not None and "cursor-agent update" in skew.fix


async def test_no_skew_check_without_both_sides(tmp_path: Path) -> None:
    checks = by_name(await local.version_checks(Runner({}), [cursor_app(tmp_path, datetime(2026, 9, 30))]))
    assert "Cursor CLI vs desktop" not in checks
    assert local.cli_date("not a date") is None
    assert local.cli_date("2026.13.40") is None


def test_hooks_go_from_missing_to_current_to_stale() -> None:
    config = RelayConfig()
    checks = by_name(local.hook_checks(config, PYTHON))
    assert all(c.status == "warn" and "not installed" in c.detail for c in checks.values())
    writes = [
        *copilot_hooks.plan(config, PYTHON),
        *cursor_hooks.plan(config, PYTHON),
        *opencode_plugin.plan(),
    ]
    apply_writes(writes, Manifest())
    assert all(c.status == "ok" for c in local.hook_checks(config, PYTHON))
    longer = RelayConfig(questions=QuestionsConfig(deadline_s=1_200))
    stale = by_name(local.hook_checks(longer, PYTHON))
    assert stale["Copilot hooks"].status == "warn"
    assert "out of date" in stale["Copilot hooks"].detail
    assert stale["OpenCode plugin"].status == "ok"


def test_mcp_check_walks_import_then_sync() -> None:
    missing = local.mcp_checks(PYTHON)
    assert missing[0].status == "warn" and missing[0].fix == "relay config import --apply"
    install_fixtures()
    plan_import(PYTHON).apply(Manifest())
    drift = local.mcp_checks(PYTHON)[0]
    assert drift.status == "warn" and drift.fix == "relay config sync --apply"
    plan_sync(python=PYTHON).apply(Manifest())
    assert local.mcp_checks(PYTHON)[0].status == "ok"


def test_mcp_check_fails_when_a_keychain_secret_is_gone(memory_keyring: MemoryKeyring) -> None:
    install_fixtures()
    plan_import(PYTHON).apply(Manifest())
    for service, account in list(memory_keyring.store):
        if service == MCP_KEYCHAIN_SERVICE:
            keyring.delete_password(service, account)
    check = local.mcp_checks(PYTHON)[0]
    assert check.status == "fail"
    assert "fake-" not in check.detail


def test_skill_checks_find_duplicates_and_missing_pieces() -> None:
    shared = paths.agents_skills_dir() / "demo"
    shared.mkdir(parents=True)
    (shared / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
    copy = paths.cursor_dir() / "skills" / "demo"
    copy.mkdir(parents=True)
    (copy / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
    checks = by_name(local.skill_checks())
    assert checks["Duplicates"].status == "warn"
    assert "demo" in checks["Duplicates"].detail
    assert checks["agent-relay-away"].status == "warn"
    assert checks["stepping-away"].status == "info"


def test_stepping_away_unpatched_is_a_warning() -> None:
    skill = stepping_away.target()
    skill.parent.mkdir(parents=True)
    writes, _ = stepping_away.plan()
    assert writes == []
    skill.write_text("someone else's skill\n", encoding="utf-8")
    assert by_name(local.skill_checks())["stepping-away"].detail.startswith("edited since")


def test_keychain_checks_follow_the_config() -> None:
    checks = by_name(local.keychain_checks(RelayConfig()))
    assert checks["webex-bot-token"].status == "fail"
    assert checks["webex-bot-token"].fix == "pbpaste | relay setup token --stdin"
    assert checks["opencode-password"].status == "warn"
    assert "pwa-token" not in checks
    keyring.set_password(KEYCHAIN_SERVICE, Secret.WEBEX_BOT_TOKEN.value, "fake-bot-value-0000")
    with_pwa = by_name(local.keychain_checks(RelayConfig(pwa=PwaConfig(enabled=True))))
    assert with_pwa["webex-bot-token"].status == "ok"
    assert with_pwa["pwa-token"].status == "fail"
    assert all("fake-bot-value" not in c.detail for c in with_pwa.values())


def test_keychain_checks_say_locked_rather_than_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def locked(service: str, username: str) -> str:
        raise keyring.errors.KeyringLocked("(-25308, 'Unknown Error')")

    monkeypatch.setattr(keyring, "get_password", locked)
    checks = by_name(local.keychain_checks(RelayConfig()))
    token = checks["webex-bot-token"]
    assert token.status == "fail"
    assert "locked" in token.detail and "missing" not in token.detail
    assert token.fix == "security unlock-keychain ~/Library/Keychains/login.keychain-db"


async def test_launchd_checks_tell_loaded_from_missing() -> None:
    runner = Runner({"io.github.cyberquill.agent-relay.relayd": (0, "state = running")})
    checks = by_name(await local.launchd_checks(PYTHON, runner))
    assert checks["relayd"].status == "ok"
    assert checks["opencode"].status == "warn"
    assert checks["opencode"].fix == "relay install --apply"
    assert all(call[:2] == ["launchctl", "print"] for call in runner.calls)


async def test_launchd_checks_report_opencode_off_without_asking_launchd() -> None:
    runner = Runner({"io.github.cyberquill.agent-relay.relayd": (0, "state = running")})
    checks = by_name(await local.launchd_checks(PYTHON, runner, opencode=False))
    assert checks["opencode"].status == "info" and "adapters.opencode" in checks["opencode"].detail
    assert not any("opencode" in call[-1] for call in runner.calls)


def webex_http(status: int, seen: list[httpx.Request] | None = None) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if status == 0:
            raise httpx.ConnectError("no route to host")
        return httpx.Response(status, json={"id": "bot", "message": "x"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_doctor_reports_rejected_token() -> None:
    keyring.set_password(KEYCHAIN_SERVICE, Secret.WEBEX_BOT_TOKEN.value, "stale-token-0000")
    seen: list[httpx.Request] = []
    async with webex_http(401, seen) as http:
        [check] = await local.webex_token_check(RelayConfig(), http)
    assert check.status == "fail" and "rejected (401)" in check.detail
    assert check.fix == "pbpaste | relay setup token --stdin"
    assert [(r.method, r.url.path) for r in seen] == [("GET", "/v1/people/me")]
    assert seen[0].headers["authorization"] == "Bearer stale-token-0000"
    assert "stale-token-0000" not in check.detail and "stale-token-0000" not in (check.fix or "")


async def test_doctor_accepts_a_working_token() -> None:
    keyring.set_password(KEYCHAIN_SERVICE, Secret.WEBEX_BOT_TOKEN.value, "good-token-0000")
    async with webex_http(200) as http:
        [check] = await local.webex_token_check(RelayConfig(), http)
    assert check.status == "ok" and "good-token" not in check.detail


@pytest.mark.parametrize(("status", "word"), [(500, "HTTP 500"), (0, "could not reach")])
async def test_doctor_only_warns_when_webex_cannot_say(status: int, word: str) -> None:
    keyring.set_password(KEYCHAIN_SERVICE, Secret.WEBEX_BOT_TOKEN.value, "any-token-0000")
    async with webex_http(status) as http:
        [check] = await local.webex_token_check(RelayConfig(), http)
    assert check.status == "warn" and word in check.detail


async def test_the_token_check_adds_nothing_when_there_is_no_token_to_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with webex_http(401) as http:
        assert await local.webex_token_check(RelayConfig(), http) == []

        def locked(service: str, username: str) -> str:
            raise keyring.errors.KeyringLocked("(-25308, 'Unknown Error')")

        monkeypatch.setattr(keyring, "get_password", locked)
        assert await local.webex_token_check(RelayConfig(), http) == []


async def test_local_checks_include_the_token_test() -> None:
    keyring.set_password(KEYCHAIN_SERVICE, Secret.WEBEX_BOT_TOKEN.value, "stale-token-0000")
    async with webex_http(401) as http:
        checks = await doctor.local_checks(RelayConfig(), PYTHON, Runner({}), http)
    token = by_name([c for c in checks if c.area == "Webex"])["Webex token"]
    assert token.status == "fail"


def test_legacy_settings_are_listed_with_the_command_that_removes_them() -> None:
    assert local.legacy_config_checks() == []
    paths.relay_toml().parent.mkdir(parents=True, exist_ok=True)
    paths.relay_toml().write_text(
        '[webex]\nallowed_email = "me@example.com"\n[webex.bots]\nvscode = "webex-bot-vscode"\n'
        "[presence]\npoll_s = 5.0\n[approvals]\ndeadline_s = 30\n",
        encoding="utf-8",
    )
    found = {c.name: c for c in local.legacy_config_checks()}
    assert set(found) == {"webex.bots", "presence.poll_s", "approvals.deadline_s"}
    assert all(c.status == "warn" for c in found.values())
    assert found["webex.bots"].fix == "relay config unset webex.bots"
