"""AC-33 (live, RELAY_LIVE=1): the real Copilot CLI and OpenCode load the bundles.

Both run with HOME, COPILOT_HOME and XDG_* pointed at a temporary directory, so nothing is
installed for the real user and no session is created.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agent_relay.models.config import RelayConfig
from agent_relay.ops import bundles
from tests.conftest import live

pytestmark = live


def isolated_env(root: Path) -> dict[str, str]:
    env = {**os.environ, "HOME": str(root / "home"), "COPILOT_HOME": str(root / "copilot")}
    for name in ("CONFIG", "DATA", "STATE", "CACHE"):
        env[f"XDG_{name}_HOME"] = str(root / f"xdg-{name.lower()}")
    (root / "home").mkdir(parents=True, exist_ok=True)
    return env


def run(argv: list[str], env: dict[str, str], cwd: Path) -> str:
    done = subprocess.run(argv, env=env, cwd=cwd, capture_output=True, text=True, timeout=120, check=False)
    assert done.returncode == 0, done.stderr[-2000:]
    return done.stdout


@pytest.fixture
def built(tmp_path: Path) -> dict[str, Path]:
    return bundles.build_all(tmp_path / "dist", RelayConfig(), sys.executable)


def test_copilot_cli_installs_the_bundle(built: dict[str, Path], tmp_path: Path) -> None:
    copilot = shutil.which("copilot")
    assert copilot, "the GitHub Copilot CLI is not on PATH"
    env = isolated_env(tmp_path)
    installed = run([copilot, "plugin", "install", str(built["copilot"])], env, tmp_path)
    assert 'Plugin "agent-relay" installed successfully' in installed
    assert "Installed 1 skill" in installed
    assert "agent-relay (v" in run([copilot, "plugin", "list"], env, tmp_path)
    copy = Path(env["COPILOT_HOME"]) / "installed-plugins" / "_direct" / "agent-relay"
    assert (copy / "hooks.json").is_file() and (copy / ".mcp.json").is_file()


def test_opencode_loads_plugin_commands_and_skill(built: dict[str, Path], tmp_path: Path) -> None:
    opencode = Path(shutil.which("opencode") or Path.home() / ".opencode" / "bin" / "opencode")
    assert opencode.is_file(), f"OpenCode is not installed at {opencode}"
    env = isolated_env(tmp_path)
    config = Path(env["XDG_CONFIG_HOME"]) / "opencode"
    shutil.copytree(built["opencode"], config)
    loaded = json.loads(run([str(opencode), "debug", "config"], env, tmp_path))
    assert loaded["plugin"] == [(config / "plugins" / "agent-relay.ts").as_uri()]
    assert set(loaded["command"]) >= set(bundles.COMMANDS)
    assert "relay away on" in loaded["command"]["away"]["template"]
    skills = json.loads(run([str(opencode), "debug", "skill"], env, tmp_path))
    assert bundles.SKILL_NAME in {skill["name"] for skill in skills}
