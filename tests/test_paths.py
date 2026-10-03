"""AC-1: only paths.py builds paths from the home directory."""

from __future__ import annotations

import re
from pathlib import Path

from agent_relay import paths

ROOT = Path(__file__).resolve().parents[1]
_HOME_USE = re.compile(r"Path\.home\(\)|expanduser\(|os\.environ\[?.?[\"']HOME|getenv\([\"']HOME|[\"']~/")


def test_only_paths_module_touches_home() -> None:
    offenders = []
    for file in (ROOT / "agent_relay").rglob("*.py"):
        if file.name == "paths.py" and file.parent.name == "agent_relay":
            continue
        for n, line in enumerate(file.read_text(encoding="utf-8").splitlines(), 1):
            if _HOME_USE.search(line):
                offenders.append(f"{file.relative_to(ROOT)}:{n}: {line.strip()}")
    assert offenders == []


def test_relay_home_overrides_everything(relay_home: Path) -> None:
    for fn in (
        paths.config_dir,
        paths.app_support,
        paths.socket_path,
        paths.db_path,
        paths.logs_dir,
        paths.copilot_dir,
        paths.cursor_dir,
        paths.opencode_config_dir,
        paths.agents_skills_dir,
        paths.vscode_user_dir,
        paths.launch_agents_dir,
    ):
        assert relay_home in fn().parents, fn.__name__


def test_expand_user(relay_home: Path) -> None:
    assert paths.expand_user("~/x") == relay_home / "x"
    assert paths.expand_user("~") == relay_home
    assert paths.expand_user("/abs") == Path("/abs")


def test_socket_path_fits_sun_path() -> None:
    assert len(str(paths.socket_path())) < 104
