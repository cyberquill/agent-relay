"""The stepping-away patch applies only over the exact file it was written against."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from agent_relay.fileops import Manifest, apply_writes, restore
from agent_relay.ops import stepping_away
from agent_relay.ops.stepping_away import PatchState

ORIGINAL = b"---\nname: stepping-away\n---\n\n# Stepping Away\n\nPost to Webex with sa.py.\n"


@pytest.fixture
def patch_dir(tmp_path: Path) -> Path:
    folder = tmp_path / "patch"
    folder.mkdir()
    (folder / "SKILL.md").write_bytes(b"---\nname: stepping-away\n---\n\nAgent Relay posts.\n")
    (folder / "base.sha256").write_text(f"{hashlib.sha256(ORIGINAL).hexdigest()}\n", encoding="utf-8")
    return folder


def install(content: bytes) -> Path:
    skill = stepping_away.target()
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_bytes(content)
    return skill


def test_target_is_under_relay_home() -> None:
    assert stepping_away.target().parts[-3:] == ("skills", "stepping-away", "SKILL.md")


def test_pending_patch_is_planned_and_restorable(patch_dir: Path) -> None:
    skill = install(ORIGINAL)
    writes, note = stepping_away.plan(patch_dir=patch_dir)
    assert stepping_away.state(patch_dir=patch_dir) is PatchState.PENDING
    assert [w.path for w in writes] == [skill]
    assert "patched" in note
    manifest = Manifest()
    apply_writes(writes, manifest)
    assert skill.read_bytes() == stepping_away.patched_content(patch_dir)
    assert stepping_away.state(patch_dir=patch_dir) is PatchState.APPLIED
    restore(manifest)
    assert skill.read_bytes() == ORIGINAL


def test_applied_patch_plans_nothing(patch_dir: Path) -> None:
    install(stepping_away.patched_content(patch_dir))
    writes, note = stepping_away.plan(patch_dir=patch_dir)
    assert writes == []
    assert note == "stepping-away is already patched"


def test_drifted_skill_is_left_alone(patch_dir: Path) -> None:
    install(ORIGINAL + b"\nA local edit.\n")
    writes, note = stepping_away.plan(patch_dir=patch_dir)
    assert writes == []
    assert stepping_away.state(patch_dir=patch_dir) is PatchState.DRIFTED
    assert "merge by hand" in note


def test_missing_skill_plans_nothing(patch_dir: Path) -> None:
    writes, note = stepping_away.plan(patch_dir=patch_dir)
    assert writes == []
    assert "not installed" in note


def test_committed_patch_keeps_rules_and_drops_webex_posting() -> None:
    text = stepping_away.patched_content().decode("utf-8")
    assert text.startswith("---\nname: stepping-away\n")
    assert "agent-relay-away" in text
    assert "reference/away-mode.md" in text
    for gone in ("webex-create-message", "webex-create-thread-reply", "sa.py post"):
        assert gone not in text
    assert len(stepping_away.base_hash()) == 64
