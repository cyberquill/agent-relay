"""`relay install|uninstall|start|stop|status|away|ws|config set|selftest`, and relayd's start-up post."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from agent_relay import paths
from agent_relay.cli import app
from agent_relay.cli import daemon as cli_daemon
from agent_relay.core.registry import Registry
from agent_relay.daemon import Core, online_message
from agent_relay.ipc import JsonObj
from agent_relay.models.config import AdaptersConfig, RelayConfig
from agent_relay.models.events import Harness, Origin
from agent_relay.models.sessions import SessionRecord, SessionStatus
from agent_relay.ops import launchd
from agent_relay.settings import load_config, load_workspaces
from tests.sync.helpers import FAKE_SECRETS, install_fixtures

runner = CliRunner()


@pytest.fixture(autouse=True)
def no_vscode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(paths, "VSCODE_CLI", tmp_path / "no-code")


def test_install_is_a_dry_run_that_hides_secrets() -> None:
    install_fixtures()
    result = runner.invoke(app, ["install", "--diff"])
    assert result.exit_code == 0, result.output
    assert "Dry run" in result.output
    assert "io.github.cyberquill.agent-relay.relayd" in result.output
    assert not paths.mcp_yaml().exists() and not paths.install_manifest().exists()
    assert [s for s in FAKE_SECRETS if s in result.output] == []


def test_uninstall_with_nothing_installed_says_so() -> None:
    result = runner.invoke(app, ["uninstall", "--apply"])
    assert result.exit_code == 0
    assert "Nothing is installed." in result.output


def test_config_set_validates_and_saves() -> None:
    assert runner.invoke(app, ["config", "set", "webex.allowed_email", "me@example.com"]).exit_code == 0
    assert runner.invoke(app, ["config", "set", "pwa.port", "9000"]).exit_code == 0
    assert runner.invoke(app, ["config", "set", "adapters.opencode", "false"]).exit_code == 0
    config = load_config()
    assert (config.webex.allowed_email, config.pwa.port, config.adapters.opencode) == (
        "me@example.com",
        9000,
        False,
    )


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("webex.nope", "1", "unknown setting"),
        ("nope.port", "1", "unknown section"),
        ("pwa.port", "not-a-port", "pwa.port"),
        ("approvals.auto_allow", '["*"]', "global auto-approve"),
        ("approvals.auto_allow", '["read", "?*"]', "global auto-approve"),
    ],
)
def test_config_set_rejects_bad_values_and_leaves_the_file(key: str, value: str, message: str) -> None:
    result = runner.invoke(app, ["config", "set", key, value])
    assert result.exit_code == 1
    assert message in result.output
    assert not paths.relay_toml().exists()


def test_workspaces_add_list_and_remove(tmp_path: Path) -> None:
    folder = tmp_path / "proj"
    folder.mkdir()
    assert runner.invoke(app, ["ws", "add", "proj", str(folder)]).exit_code == 0
    duplicate = runner.invoke(app, ["ws", "add", "proj", str(folder)])
    assert duplicate.exit_code == 1 and "relay ws rm proj" in duplicate.output
    missing = runner.invoke(app, ["ws", "add", "other", str(tmp_path / "absent")])
    assert missing.exit_code == 1 and "not a directory" in missing.output
    listed = runner.invoke(app, ["ws", "list"])
    assert f"proj\t{folder.resolve()}" in listed.output
    assert runner.invoke(app, ["ws", "rm", "proj"]).exit_code == 0
    assert load_workspaces().workspaces == []
    assert runner.invoke(app, ["ws", "rm", "proj"]).exit_code == 1


def test_workspaces_add_shares_the_phone_name_rule(tmp_path: Path) -> None:
    for bad in ("two words", "x" * 41, "a/b"):
        result = runner.invoke(app, ["ws", "add", bad, str(tmp_path)])
        assert result.exit_code == 1 and "1 to 40 characters" in result.output, bad
    assert load_workspaces().workspaces == []


def test_workspaces_add_a_code_workspace_file(tmp_path: Path) -> None:
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
    file = tmp_path / "team.code-workspace"
    file.write_text('{"folders": [{"path": "a"}, {"path": "b"}], // comment\n}')
    assert runner.invoke(app, ["ws", "add", "team", str(file)]).exit_code == 0
    [entry] = load_workspaces().workspaces
    assert (entry.path, entry.file) == (str((tmp_path / "a").resolve()), str(file.resolve()))
    assert f"team\t{file.resolve()}" in runner.invoke(app, ["ws", "list"]).output
    gone = tmp_path / "gone.code-workspace"
    gone.write_text('{"folders": [{"path": "missing"}]}')
    result = runner.invoke(app, ["ws", "add", "gone", str(gone)])
    assert result.exit_code == 1 and "is not a directory" in result.output


def test_away_without_relayd_points_at_relay_start() -> None:
    result = runner.invoke(app, ["away", "on"])
    assert result.exit_code == 1
    assert "relay start" in result.output


def test_away_sends_the_resolved_workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sent: list[JsonObj] = []

    def fake(message: JsonObj, sock_path: Path, timeout: float | None) -> JsonObj:
        sent.append(message)
        return {"away": message["enabled"]}

    monkeypatch.setattr(cli_daemon, "request", fake)
    result = runner.invoke(app, ["away", "off", "--workspace", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert sent == [{"op": "away", "enabled": False, "workspace": str(tmp_path.resolve())}]
    assert f"away mode off for {tmp_path.resolve()}" in result.output


def test_status_renders_the_daemon_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    reply: JsonObj = {"sessions": 2, "parked": 1, "pending": 0, "adapters": {"opencode": 0}, "paused": True}
    monkeypatch.setattr(cli_daemon, "request", lambda message, sock_path, timeout: reply)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "sessions 2, parked 1, pending 0" in result.output
    assert "paused" in result.output


class Launchctl:
    """`lingers`: how many `print` calls still find the service after a bootout, as launchd does."""

    def __init__(self, loaded: bool, lingers: int = 0) -> None:
        self.loaded = loaded
        self.lingers = lingers
        self.seen: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> tuple[int, str]:
        self.seen.append(list(argv))
        if argv[1] == "bootout":
            self.loaded = False
        if argv[1] == "print" and not self.loaded:
            if self.lingers > 0:
                self.lingers -= 1
                return 0, ""
            return 113, "Could not find service"
        return 0, ""


def test_start_needs_the_launch_agent() -> None:
    result = runner.invoke(app, ["start"])
    assert result.exit_code == 1
    assert "relay install --apply" in result.output


def test_start_bootstraps_when_unloaded_then_kickstarts() -> None:
    agent = cli_daemon.relayd_agent()
    agent.path.parent.mkdir(parents=True)
    agent.path.write_bytes(b"<plist/>")
    calls = Launchctl(loaded=False)
    cli_daemon.start_daemon(calls)
    assert [c[1] for c in calls.seen] == ["print", "enable", "bootstrap", "kickstart"]
    loaded = Launchctl(loaded=True)
    cli_daemon.start_daemon(loaded)
    assert [c[1] for c in loaded.seen] == ["print", "kickstart"]
    stopping = Launchctl(loaded=True, lingers=2)
    naps: list[float] = []
    cli_daemon.stop_daemon(stopping, sleep=naps.append)
    assert stopping.seen[0][:2] == ["launchctl", "bootout"], "kill would be undone by KeepAlive"
    assert [c[1] for c in stopping.seen] == ["bootout", "print", "print", "print"]
    assert len(naps) == 2, "stop returns only once launchd has let go, so start can bootstrap"


def test_stop_gives_up_waiting_with_a_note(capsys: pytest.CaptureFixture[str]) -> None:
    stuck = Launchctl(loaded=True, lingers=10_000)
    cli_daemon.stop_daemon(stuck, sleep=lambda _: None)
    assert len(stuck.seen) == 1 + launchd.UNLOAD_POLLS
    assert "give it a moment" in capsys.readouterr().err


class PerLabel:
    """A launchctl that tracks each label separately, as launchd does."""

    def __init__(self, loaded: set[str]) -> None:
        self.loaded = loaded
        self.seen: list[list[str]] = []

    def label(self, argv: Sequence[str]) -> str:
        return Path(argv[-1]).stem if argv[1] == "bootstrap" else argv[-1].rsplit("/", 1)[-1]

    def __call__(self, argv: Sequence[str]) -> tuple[int, str]:
        self.seen.append(list(argv))
        label = self.label(argv)
        if argv[1] == "print":
            return (0, "") if label in self.loaded else (113, "Could not find service")
        if argv[1] == "bootout":
            if label not in self.loaded:
                return 3, "No such process"
            self.loaded.discard(label)
        if argv[1] == "bootstrap":
            self.loaded.add(label)
        return 0, ""

    def verbs(self, verb: str) -> list[str]:
        return [self.label(a) for a in self.seen if a[1] == verb]


def _install_plists(*names: str) -> None:
    for agent in launchd.agents("python"):
        if agent.name in names:
            agent.path.parent.mkdir(parents=True, exist_ok=True)
            agent.path.write_bytes(b"<plist/>")


RELAYD = f"{launchd.LABEL_PREFIX}.relayd"
OPENCODE = f"{launchd.LABEL_PREFIX}.opencode"
UPDATE = f"{launchd.LABEL_PREFIX}.cursor-update"


def test_stop_boots_out_every_keep_alive_agent_but_not_the_weekly_update() -> None:
    _install_plists("relayd", "opencode", "cursor-update")
    calls = PerLabel({RELAYD, OPENCODE, UPDATE})
    message = cli_daemon.stop_daemon(calls, sleep=lambda _: None)
    assert calls.verbs("bootout") == [RELAYD, OPENCODE]
    assert calls.loaded == {UPDATE}
    assert RELAYD in message and OPENCODE in message and UPDATE not in message


def test_stop_skips_an_opencode_agent_that_is_not_installed() -> None:
    _install_plists("relayd")
    calls = PerLabel({RELAYD})
    cli_daemon.stop_daemon(calls, sleep=lambda _: None)
    assert calls.verbs("bootout") == [RELAYD]


def test_stop_still_stops_opencode_when_relayd_is_already_down() -> None:
    _install_plists("relayd", "opencode")
    calls = PerLabel({OPENCODE})
    message = cli_daemon.stop_daemon(calls, sleep=lambda _: None)
    assert calls.loaded == set() and OPENCODE in message and RELAYD not in message


def test_stop_fails_when_nothing_is_loaded(capsys: pytest.CaptureFixture[str]) -> None:
    _install_plists("relayd", "opencode")
    with pytest.raises(typer.Exit):
        cli_daemon.stop_daemon(PerLabel(set()), sleep=lambda _: None)
    assert "relayd is not loaded" in capsys.readouterr().err


def test_start_starts_the_opencode_agent_too_when_it_is_installed() -> None:
    _install_plists("relayd", "opencode", "cursor-update")
    calls = PerLabel({RELAYD})
    message = cli_daemon.start_daemon(calls)
    assert calls.verbs("bootstrap") == [OPENCODE], "relayd was already loaded"
    assert calls.verbs("kickstart") == [RELAYD, OPENCODE]
    assert RELAYD in message and OPENCODE in message and UPDATE not in message


def test_start_without_the_opencode_agent_starts_relayd_alone() -> None:
    _install_plists("relayd")
    calls = PerLabel(set())
    cli_daemon.start_daemon(calls)
    assert calls.verbs("kickstart") == [RELAYD]


def test_start_stops_and_disables_opencode_while_its_adapter_is_off() -> None:
    _install_plists("relayd", "opencode")
    calls = PerLabel({RELAYD, OPENCODE})
    config = RelayConfig(adapters=AdaptersConfig(opencode=False))
    message = cli_daemon.start_daemon(calls, config, sleep=lambda _: None)
    assert calls.verbs("bootout") == [OPENCODE] and calls.verbs("disable") == [OPENCODE]
    assert calls.verbs("kickstart") == [RELAYD] and calls.loaded == {RELAYD}
    assert f"stopped and disabled {OPENCODE}" in message
    again = PerLabel({RELAYD})
    cli_daemon.start_daemon(again, config, sleep=lambda _: None)
    assert again.verbs("bootout") == [] and again.verbs("disable") == [OPENCODE]


def test_start_enables_opencode_again_before_bootstrapping_it() -> None:
    _install_plists("relayd", "opencode")
    calls = PerLabel({RELAYD})
    cli_daemon.start_daemon(calls, RelayConfig())
    verbs = [c[1] for c in calls.seen if calls.label(c) == OPENCODE]
    assert verbs == ["print", "enable", "bootstrap", "kickstart"]


def test_cdp_selftest_refuses_while_the_adapter_is_off() -> None:
    result = runner.invoke(app, ["selftest", "cursor-cdp"])
    assert result.exit_code == 1
    assert "relay config set cdp.enabled true" in result.output


async def test_away_off_over_ipc_releases_parked_sessions(registry: Registry) -> None:
    core = Core(RelayConfig(), registry)
    session = SessionRecord(
        session_key="cursor:K1", harness=Harness.CURSOR, harness_id="K1", workspace="/w", origin=Origin.DESK
    )
    await registry.create_session(session)
    await registry.set_session_status(session.session_key, SessionStatus.PARKED)
    assert await core.handle_ipc({"op": "away", "enabled": True, "workspace": "/w"}) == {"away": True}
    assert await core.handle_ipc({"op": "away", "enabled": False, "workspace": "/w"}) == {"away": False}
    assert await registry.kv_get("release:cursor:K1") == "away mode off"


def test_online_message_mentions_recovery_and_pause() -> None:
    assert online_message(0) == "🖥️ **Mac online** — relayd started."
    paths.pause_flag().parent.mkdir(parents=True)
    paths.pause_flag().write_text("now")
    text = online_message(2)
    assert "2 session(s) were parked" in text
    assert "`/away on` resumes" in text
