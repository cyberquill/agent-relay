"""AC-36: only byte-identical per-harness skill folders are moved aside; everything else is reported."""

from __future__ import annotations

import os
from pathlib import Path

from agent_relay import paths
from agent_relay.fileops import Manifest, restore
from agent_relay.sync.skills import fingerprint, harness_skill_dirs, plan_dedupe

STAMP = "20261003T120000Z"


def skill(root: Path, name: str, body: str = "body", extra: dict[str, str] | None = None) -> Path:
    folder = root / name
    (folder / "reference").mkdir(parents=True)
    (folder / "SKILL.md").write_text(f"---\nname: {name}\n---\n{body}\n", encoding="utf-8")
    (folder / "reference" / "notes.md").write_text("notes", encoding="utf-8")
    for rel, text in (extra or {}).items():
        (folder / rel).write_text(text, encoding="utf-8")
    return folder


def test_identical_copies_move_and_others_stay() -> None:
    canonical = paths.agents_skills_dir()
    skill(canonical, "same")
    skill(canonical, "edited")
    skill(canonical, "extra-file")
    dirs = harness_skill_dirs()
    same_copies = [skill(dirs[h], "same") for h in ("copilot", "cursor", "claude", "opencode")]
    edited = skill(dirs["cursor"], "edited", body="changed")
    extra = skill(dirs["claude"], "extra-file", extra={"more.md": "x"})
    unique = skill(dirs["copilot"], "only-here")

    plan = plan_dedupe(STAMP)

    assert sorted(m.source for m in plan.moves) == sorted(same_copies)
    assert sorted(plan.different) == sorted([edited, extra])
    assert plan.unique == [unique]
    destinations = {m.destination for m in plan.moves}
    assert paths.skill_dup_backup_dir() / STAMP / "cursor" / "same" in destinations
    report = "\n".join(plan.report())
    assert f"keep {edited}: differs" in report
    assert f"keep {unique}: no ~/.agents/skills/only-here" in report


def test_apply_moves_and_restore_puts_folders_back() -> None:
    canonical = paths.agents_skills_dir()
    skill(canonical, "same")
    copy = skill(harness_skill_dirs()["copilot"], "same")
    before = fingerprint(copy)
    manifest = Manifest()
    moved = plan_dedupe(STAMP).apply(manifest)
    assert moved == [copy]
    assert not copy.exists()
    assert fingerprint(paths.skill_dup_backup_dir() / STAMP / "copilot" / "same") == before
    restore(manifest)
    assert fingerprint(copy) == before


def test_a_dry_run_moves_nothing() -> None:
    skill(paths.agents_skills_dir(), "same")
    copy = skill(harness_skill_dirs()["opencode"], "same")
    assert plan_dedupe(STAMP).moves
    assert copy.is_dir()


def test_symlinks_are_compared_by_target_and_never_followed() -> None:
    canonical = skill(paths.agents_skills_dir(), "linked")
    os.symlink("reference/notes.md", canonical / "alias.md")
    copy = skill(harness_skill_dirs()["cursor"], "linked")
    os.symlink("SKILL.md", copy / "alias.md")
    plan = plan_dedupe(STAMP)
    assert plan.moves == [] and plan.different == [copy]


def test_symlinked_skill_entries_and_folders_are_left_alone(tmp_path: Path) -> None:
    canonical = skill(paths.agents_skills_dir(), "same")
    folder = harness_skill_dirs()["copilot"]
    folder.mkdir(parents=True)
    os.symlink(canonical, folder / "same")
    other = tmp_path / "elsewhere"
    other.mkdir()
    cursor_skills = harness_skill_dirs()["cursor"]
    cursor_skills.parent.mkdir(parents=True)
    os.symlink(other, cursor_skills)
    plan = plan_dedupe(STAMP)
    assert plan.moves == [] and plan.different == [] and plan.unique == []


def test_canonical_skill_may_itself_be_a_symlink(tmp_path: Path) -> None:
    real = skill(tmp_path, "relay-skill")
    canonical_root = paths.agents_skills_dir()
    canonical_root.mkdir(parents=True)
    os.symlink(real, canonical_root / "relay-skill")
    copy = skill(harness_skill_dirs()["claude"], "relay-skill")
    assert [m.source for m in plan_dedupe(STAMP).moves] == [copy]
