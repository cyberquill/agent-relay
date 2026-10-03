"""Planned file changes with diffs, timestamped backups and a manifest for byte-exact restore.

Every change to a file outside the repo goes through here: hook installers, MCP sync, skills
dedupe, launchd agents. The manifest records the state before the relay's *first* change to a
path, so installing twice keeps the original backup and uninstall restores it exactly.
"""

from __future__ import annotations

import difflib
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from agent_relay import paths

Action = Literal["created", "modified", "moved"]


class PlannedWrite(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: Path
    content: bytes
    mode: int = 0o644
    reason: str = ""

    def current(self) -> bytes | None:
        return self.path.read_bytes() if self.path.is_file() else None

    def changed(self) -> bool:
        return self.current() != self.content

    def diff(self) -> str:
        before = (self.current() or b"").decode("utf-8", errors="replace").splitlines(keepends=True)
        after = self.content.decode("utf-8", errors="replace").splitlines(keepends=True)
        label = str(self.path)
        return "".join(difflib.unified_diff(before, after, f"{label} (current)", f"{label} (planned)"))


class PlannedMove(BaseModel):
    """Move a directory aside into a backup location (reversible)."""

    model_config = ConfigDict(frozen=True)

    source: Path
    destination: Path
    reason: str = ""


class ManifestEntry(BaseModel):
    action: Action
    backup: str | None = None
    mode: int | None = None
    moved_to: str | None = None
    created_dirs: list[str] = Field(default_factory=list)


class Manifest(BaseModel):
    entries: dict[str, ManifestEntry] = Field(default_factory=dict)

    @classmethod
    def load(cls, file: Path | None = None) -> Manifest:
        target = file or paths.install_manifest()
        if not target.exists():
            return cls()
        return cls.model_validate_json(target.read_text(encoding="utf-8"))

    def save(self, file: Path | None = None) -> None:
        target = file or paths.install_manifest()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.model_dump_json(indent=2), encoding="utf-8")


def timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _backup_target(path: Path, stamp: str) -> Path:
    try:
        rel = path.resolve().relative_to(paths.home().resolve())
    except ValueError:
        rel = Path(*path.resolve().parts[1:])
    return paths.backups_dir() / stamp / rel


def atomic_write(path: Path, content: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.relay-tmp")
    tmp.write_bytes(content)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def apply_writes(writes: list[PlannedWrite], manifest: Manifest, stamp: str | None = None) -> list[Path]:
    """Apply changed writes, taking a backup the first time the relay touches each path."""
    stamp = stamp or timestamp()
    applied: list[Path] = []
    for write in writes:
        if not write.changed():
            continue
        key = str(write.path)
        if key not in manifest.entries:
            if write.path.is_file():
                backup = _backup_target(write.path, stamp)
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(write.path, backup)
                manifest.entries[key] = ManifestEntry(
                    action="modified", backup=str(backup), mode=write.path.stat().st_mode & 0o777
                )
            else:
                manifest.entries[key] = ManifestEntry(
                    action="created", created_dirs=_missing_dirs(write.path)
                )
        atomic_write(write.path, write.content, write.mode)
        applied.append(write.path)
    manifest.save()
    return applied


def _missing_dirs(path: Path) -> list[str]:
    missing: list[str] = []
    current = path.parent
    while not current.exists():
        missing.append(str(current))
        current = current.parent
    return missing


def apply_moves(moves: list[PlannedMove], manifest: Manifest) -> list[Path]:
    done: list[Path] = []
    for move in moves:
        if not move.source.exists():
            continue
        move.destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(move.source), str(move.destination))
        manifest.entries[str(move.source)] = ManifestEntry(action="moved", moved_to=str(move.destination))
        done.append(move.source)
    manifest.save()
    return done


def restore(manifest: Manifest, only: set[str] | None = None) -> list[Path]:
    """Undo recorded changes. Restores backups byte for byte; removes files the relay created."""
    restored: list[Path] = []
    created_dirs: set[Path] = set()
    for key, entry in list(manifest.entries.items()):
        if only is not None and key not in only:
            continue
        target = Path(key)
        if entry.action == "created":
            if target.is_file() or target.is_symlink():
                target.unlink()
            created_dirs.update(Path(d) for d in entry.created_dirs)
        elif entry.action == "modified" and entry.backup:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(entry.backup, target)
            if entry.mode is not None:
                os.chmod(target, entry.mode)
        elif entry.action == "moved" and entry.moved_to:
            if Path(entry.moved_to).exists() and not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(entry.moved_to, str(target))
        del manifest.entries[key]
        restored.append(target)
    for directory in sorted(created_dirs, key=lambda d: len(d.parts), reverse=True):
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()
    manifest.save()
    return restored


def render_json(value: object) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode()
