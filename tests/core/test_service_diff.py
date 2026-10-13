"""/diff runs plain `git diff`: no repo-configured programs, and a capped result."""

from __future__ import annotations

import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from agent_relay.core.registry import Registry
from agent_relay.core.service import RelayError
from agent_relay.core.service import sessions as sessions_module
from agent_relay.daemon import Core
from agent_relay.models.config import RelayConfig
from agent_relay.models.events import Harness
from agent_relay.models.sessions import SessionRecord

KEY = "opencode:d1"


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    (root / "a.txt").write_text("one\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "first")
    (root / "a.txt").write_text("two\n", encoding="utf-8")
    return root


@pytest.fixture
async def core(registry: Registry, repo: Path) -> AsyncIterator[Core]:
    built = Core(RelayConfig(), registry)
    await registry.create_session(
        SessionRecord(session_key=KEY, harness=Harness.OPENCODE, harness_id="d1", workspace=str(repo))
    )
    yield built
    await built.router.stop()


async def test_diff_returns_the_working_tree_changes(core: Core) -> None:
    out = (await core.service.diff(KEY)).decode()
    assert "-one" in out and "+two" in out


async def test_diff_does_not_run_programs_named_in_the_repo_config(core: Core, repo: Path) -> None:
    marker = repo.parent / "ran"
    script = repo.parent / "evil.sh"
    script.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    script.chmod(0o755)
    git(repo, "config", "diff.external", str(script))
    git(repo, "config", "diff.evil.textconv", str(script))
    (repo / ".gitattributes").write_text("*.txt diff=evil\n", encoding="utf-8")
    out = (await core.service.diff(KEY)).decode()
    assert not marker.exists()
    assert "+two" in out


async def test_diff_output_is_capped_with_a_marker(
    core: Core, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / "a.txt").write_text("x" * 5_000 + "\n", encoding="utf-8")
    monkeypatch.setattr(sessions_module, "DIFF_LIMIT_BYTES", 1_000)
    out = await core.service.diff(KEY)
    text = out.decode()
    assert len(out) < 1_200
    assert text.rstrip().endswith("more bytes)")
    assert "… diff truncated (" in text
    declared = int(text.rsplit("(", 1)[1].split()[0])
    assert declared > 4_000


async def test_diff_reports_a_failing_git(registry: Registry, tmp_path: Path) -> None:
    built = Core(RelayConfig(), registry)
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    await registry.create_session(
        SessionRecord(session_key=KEY, harness=Harness.OPENCODE, harness_id="d1", workspace=str(plain))
    )
    with pytest.raises(RelayError, match="git diff"):
        await built.service.diff(KEY)
    await built.router.stop()
