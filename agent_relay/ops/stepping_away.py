"""The `stepping-away` skill patch: drop its Webex posting, keep its decision rules.

The patch is the whole new `SKILL.md`, applied only over the exact file it was written against
(`base.sha256`). A skill edited since then is left alone and reported, never overwritten.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from pathlib import Path

from agent_relay import paths
from agent_relay.fileops import PlannedWrite

PATCH_DIR = Path(__file__).resolve().parents[2] / "patches" / "stepping-away"


class PatchState(StrEnum):
    PENDING = "pending"
    APPLIED = "applied"
    DRIFTED = "drifted"
    MISSING = "missing"


def target() -> Path:
    return paths.agents_skills_dir() / "stepping-away" / "SKILL.md"


def patched_content(patch_dir: Path = PATCH_DIR) -> bytes:
    return (patch_dir / "SKILL.md").read_bytes()


def base_hash(patch_dir: Path = PATCH_DIR) -> str:
    return (patch_dir / "base.sha256").read_text(encoding="utf-8").strip()


def state(skill: Path | None = None, patch_dir: Path = PATCH_DIR) -> PatchState:
    file = skill or target()
    if not file.is_file():
        return PatchState.MISSING
    current = file.read_bytes()
    if current == patched_content(patch_dir):
        return PatchState.APPLIED
    if hashlib.sha256(current).hexdigest() == base_hash(patch_dir):
        return PatchState.PENDING
    return PatchState.DRIFTED


def plan(skill: Path | None = None, patch_dir: Path = PATCH_DIR) -> tuple[list[PlannedWrite], str]:
    """The write to apply (empty when nothing to do) and a one-line explanation for `relay install`."""
    file = skill or target()
    current = state(file, patch_dir)
    if current is PatchState.PENDING:
        write = PlannedWrite(
            path=file,
            content=patched_content(patch_dir),
            reason="stepping-away: Agent Relay posts to Webex now; keep only the decision rules",
        )
        return [write], "stepping-away will be patched (a backup is kept)"
    if current is PatchState.APPLIED:
        return [], "stepping-away is already patched"
    if current is PatchState.MISSING:
        return [], f"stepping-away is not installed at {file}; nothing to patch"
    return [], (
        f"stepping-away at {file} changed since the patch was written; left as is. "
        f"Compare it with {patch_dir / 'SKILL.md'} and merge by hand."
    )
