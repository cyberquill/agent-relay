"""`relay config|skills|doctor`: dry runs write nothing, --apply writes, and no secret reaches the output."""

from __future__ import annotations

import json
import shutil
from collections.abc import Sequence

import pytest
from typer.testing import CliRunner

from agent_relay import paths
from agent_relay.cli import app
from tests.sync.helpers import FAKE_SECRETS, install_fixtures

runner = CliRunner()


def leaked(text: str) -> list[str]:
    return [secret for secret in FAKE_SECRETS if secret in text]


def test_config_import_is_a_dry_run_until_apply() -> None:
    install_fixtures()
    dry = runner.invoke(app, ["config", "import"])
    assert dry.exit_code == 0, dry.output
    assert "Dry run" in dry.output
    assert not paths.mcp_yaml().exists()
    assert leaked(dry.output) == []
    applied = runner.invoke(app, ["config", "import", "--apply"])
    assert applied.exit_code == 0, applied.output
    assert f"wrote {paths.mcp_yaml()}" in applied.output
    assert leaked(paths.mcp_yaml().read_text(encoding="utf-8")) == []


def test_config_sync_refuses_without_a_catalog() -> None:
    result = runner.invoke(app, ["config", "sync"])
    assert result.exit_code == 1
    assert "relay config import --apply" in result.output


def test_config_sync_shows_redacted_diffs_then_applies() -> None:
    install_fixtures()
    assert runner.invoke(app, ["config", "import", "--apply"]).exit_code == 0
    dry = runner.invoke(app, ["config", "sync"])
    assert dry.exit_code == 0, dry.output
    assert "agent-relay" in dry.output
    assert leaked(dry.output) == []
    assert runner.invoke(app, ["config", "sync", "--apply"]).exit_code == 0
    again = runner.invoke(app, ["config", "sync"])
    assert "Everything already matches." in again.output


def test_skills_dedupe_moves_only_with_apply() -> None:
    for root in (paths.agents_skills_dir(), paths.copilot_dir() / "skills"):
        (root / "demo").mkdir(parents=True)
        (root / "demo" / "SKILL.md").write_text("same\n", encoding="utf-8")
    copy = paths.copilot_dir() / "skills" / "demo"
    dry = runner.invoke(app, ["skills", "dedupe"])
    assert dry.exit_code == 0 and "move" in dry.output and copy.exists()
    applied = runner.invoke(app, ["skills", "dedupe", "--apply"])
    assert applied.exit_code == 0, applied.output
    assert not copy.exists()
    assert runner.invoke(app, ["skills", "dedupe"]).output.strip() == "No duplicates to move."


def test_doctor_json_reports_and_fails_without_relayd(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    async def quiet(argv: Sequence[str]) -> tuple[int, str, str]:
        calls.append(list(argv))
        return 1, "", ""

    monkeypatch.setattr(shutil, "which", lambda _: None)
    monkeypatch.setattr("agent_relay.doctor.run_cli", quiet)
    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 1
    data = json.loads(result.stdout)
    assert data["ok"] is False
    assert any(c["area"] == "relayd" and c["status"] == "fail" for c in data["checks"])
    assert calls and all(call[0] == "launchctl" for call in calls)
