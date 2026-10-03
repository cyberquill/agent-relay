"""launchd templates and the opencode launcher: loopback only, the secret only in the child's env."""

from __future__ import annotations

import plistlib
from collections.abc import Mapping
from pathlib import Path

import pytest

from agent_relay import paths
from agent_relay.fileops import Manifest, apply_writes, restore, timestamp
from agent_relay.models.config import OpenCodeConfig, RelayConfig
from agent_relay.ops import launchd, launchers
from agent_relay.settings import SECRET_SETUP_COMMAND, Secret, save_config, set_secret

PY = "/repo/.venv/bin/python"


def test_three_agents_with_expected_shape(relay_home: Path) -> None:
    rendered = {a.name: plistlib.loads(a.render()) for a in launchd.agents(PY)}
    assert set(rendered) == {"relayd", "opencode", "cursor-update"}
    relayd = rendered["relayd"]
    assert relayd["Label"] == "io.github.cyberquill.agent-relay.relayd"
    assert relayd["ProgramArguments"] == [PY, "-m", "agent_relay.daemon"]
    assert relayd["KeepAlive"] is True and relayd["RunAtLoad"] is True
    assert relayd["StandardErrorPath"] == str(paths.logs_dir() / "relayd.err.log")
    assert rendered["opencode"]["ProgramArguments"] == [PY, "-m", "agent_relay.ops.launchers", "opencode"]
    update = rendered["cursor-update"]
    assert update["ProgramArguments"] == [str(paths.cursor_agent_cli()), "update"]
    assert update["StartCalendarInterval"] == launchd.WEEKLY_UPDATE and "KeepAlive" not in update
    for body in rendered.values():
        flat = plistlib.dumps(body).decode()
        assert "PASSWORD" not in flat and "token" not in flat.lower()


def test_plan_writes_into_launch_agents_and_restores(relay_home: Path) -> None:
    writes = launchd.plan(PY)
    assert {w.path.parent for w in writes} == {paths.launch_agents_dir()}
    manifest = Manifest()
    apply_writes(writes, manifest, timestamp())
    assert all(w.path.exists() and not w.changed() for w in writes)
    restore(manifest)
    assert not any(w.path.exists() for w in writes)
    assert not paths.launch_agents_dir().exists()


def test_launchctl_commands() -> None:
    agent = launchd.agents(PY)[0]
    assert launchd.bootstrap_command(agent, uid=501) == ["launchctl", "bootstrap", "gui/501", str(agent.path)]
    assert launchd.bootout_command(agent, uid=501) == [
        "launchctl",
        "bootout",
        "gui/501/io.github.cyberquill.agent-relay.relayd",
    ]


@pytest.mark.parametrize("url", ["http://0.0.0.0:4096", "http://192.168.1.5:4096", "http://example.com"])
def test_opencode_refuses_non_loopback(url: str) -> None:
    with pytest.raises(launchers.LaunchError):
        launchers.opencode_argv(OpenCodeConfig(url=url), Path("/bin/opencode"))


def test_opencode_argv_and_env() -> None:
    argv = launchers.opencode_argv(OpenCodeConfig(url="http://127.0.0.1:4555"), Path("/bin/opencode"))
    assert argv == ["/bin/opencode", "serve", "--hostname", "127.0.0.1", "--port", "4555"]
    env = launchers.opencode_env({"PATH": "/bin", "OPENCODE_SERVER_PASSWORD": "stale"}, "fresh")
    assert env == {"PATH": "/bin", "OPENCODE_SERVER_PASSWORD": "fresh"}


class Captured:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str], Mapping[str, str]]] = []

    def __call__(self, file: str, argv: list[str], env: Mapping[str, str]) -> None:
        self.calls.append((file, argv, env))


def _fake_binary(relay_home: Path) -> Path:
    binary = paths.opencode_cli()
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    return binary


def test_launcher_execs_with_keychain_password(relay_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    binary = _fake_binary(relay_home)
    save_config(RelayConfig())
    set_secret(Secret.OPENCODE_PASSWORD, "pw-from-keychain")
    captured = Captured()
    assert launchers.opencode_main(captured) == 0
    file, argv, env = captured.calls[0]
    assert file == str(binary) and argv[1:3] == ["serve", "--hostname"]
    assert env["OPENCODE_SERVER_PASSWORD"] == "pw-from-keychain"
    out = capsys.readouterr()
    assert "pw-from-keychain" not in out.out + out.err


def test_launcher_without_password_or_binary(relay_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    captured = Captured()
    assert launchers.opencode_main(captured) == launchers.EXIT_CONFIG
    assert SECRET_SETUP_COMMAND[Secret.OPENCODE_PASSWORD] in capsys.readouterr().err
    set_secret(Secret.OPENCODE_PASSWORD, "x")
    assert launchers.opencode_main(captured) == launchers.EXIT_CONFIG
    assert "install OpenCode first" in capsys.readouterr().err
    assert captured.calls == []
    assert launchers.main(["bogus"]) == launchers.EXIT_CONFIG
