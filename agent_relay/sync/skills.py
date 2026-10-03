"""`relay skills dedupe`: move per-harness skill copies aside when they equal `~/.agents/skills/<name>`."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from agent_relay import paths
from agent_relay.fileops import Manifest, PlannedMove, apply_moves, timestamp

Fingerprint = dict[str, tuple[str, bytes]]


def harness_skill_dirs() -> dict[str, Path]:
    return {
        "copilot": paths.copilot_dir() / "skills",
        "cursor": paths.cursor_dir() / "skills",
        "claude": paths.claude_dir() / "skills",
        "opencode": paths.opencode_config_dir() / "skills",
    }


def fingerprint(root: Path) -> Fingerprint:
    """Every entry under `root` by relative path: files by bytes, symlinks by target. No following links."""
    found: Fingerprint = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        for name in [*dirnames, *filenames]:
            entry = here / name
            rel = entry.relative_to(root).as_posix()
            if entry.is_symlink():
                found[rel] = ("link", os.readlink(entry).encode())
            elif entry.is_dir():
                found[rel] = ("dir", b"")
            else:
                found[rel] = ("file", entry.read_bytes())
    return found


def identical(left: Path, right: Path) -> bool:
    return fingerprint(left) == fingerprint(right)


@dataclass
class DedupePlan:
    moves: list[PlannedMove] = field(default_factory=list)
    different: list[Path] = field(default_factory=list)
    unique: list[Path] = field(default_factory=list)

    def report(self) -> list[str]:
        lines = [f"move {m.source} → {m.destination}" for m in self.moves]
        lines += [f"keep {p}: differs from ~/.agents/skills/{p.name}" for p in self.different]
        lines += [f"keep {p}: no ~/.agents/skills/{p.name}" for p in self.unique]
        return lines

    def apply(self, manifest: Manifest | None = None) -> list[Path]:
        return apply_moves(self.moves, manifest if manifest is not None else Manifest.load())


def plan_dedupe(stamp: str | None = None) -> DedupePlan:
    canonical = paths.agents_skills_dir()
    backup = paths.skill_dup_backup_dir() / (stamp or timestamp())
    plan = DedupePlan()
    for harness, folder in harness_skill_dirs().items():
        if not folder.is_dir() or folder.is_symlink():
            continue
        for skill in sorted(folder.iterdir()):
            if skill.is_symlink() or not skill.is_dir():
                continue
            original = canonical / skill.name
            if not original.is_dir():
                plan.unique.append(skill)
            elif identical(skill, original):
                plan.moves.append(
                    PlannedMove(
                        source=skill,
                        destination=backup / harness / skill.name,
                        reason=f"identical to {original}",
                    )
                )
            else:
                plan.different.append(skill)
    return plan
