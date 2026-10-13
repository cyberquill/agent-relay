"""Planned file changes with diffs, timestamped backups and a manifest for a lossless uninstall.

Every change to a file outside the repo goes through here: hook installers, MCP sync, skills
dedupe, launchd agents. The manifest records the state before the relay's *first* change to a
path and a digest of its *last* write, so installing twice keeps the original backup and
uninstall restores it only over a file nobody has edited since.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from agent_relay import paths

Action = Literal["created", "modified", "moved"]
Fate = Literal["remove", "restore", "unmerge", "leave", "move back"]
# Removes the relay's entries from a file's current bytes; raises ValueError when they cannot be parsed.
type Unmerge = Callable[[bytes], bytes]


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


class PlannedLink(BaseModel):
    """A symlink the relay owns. An existing non-link at the path is never replaced."""

    model_config = ConfigDict(frozen=True)

    path: Path
    target: Path
    reason: str = ""

    def changed(self) -> bool:
        return not (self.path.is_symlink() and Path(os.readlink(self.path)) == self.target)

    def blocked(self) -> bool:
        return self.path.exists() and not self.path.is_symlink()


class LinkBlockedError(RuntimeError):
    def __init__(self, path: Path) -> None:
        super().__init__(f"{path} exists and is not a symlink; move it aside and run the install again")
        self.path = path


class ManifestEntry(BaseModel):
    action: Action
    backup: str | None = None
    mode: int | None = None
    moved_to: str | None = None
    created_dirs: list[str] = Field(default_factory=list)
    written_sha256: str | None = None


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
        atomic_write(target, self.model_dump_json(indent=2).encode(), 0o600)


def timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _backup_target(path: Path, stamp: str) -> Path:
    try:
        rel = path.resolve().relative_to(paths.home().resolve())
    except ValueError:
        rel = Path(*path.resolve().parts[1:])
    return paths.backups_dir() / stamp / rel


def atomic_write(path: Path, content: bytes, mode: int) -> None:
    """Write through a symlink, via a temp file that has its final mode from the start."""
    real = path.resolve() if path.is_symlink() else path
    real.parent.mkdir(parents=True, exist_ok=True)
    tmp = real.with_name(f".{real.name}.relay-tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            # The umask can only narrow the open() mode, so this widens to `mode` and never beyond it.
            os.fchmod(handle.fileno(), mode)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, real)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _private_backup(source: Path, backup: Path) -> None:
    """Backups can hold plaintext secrets: every directory under the backups root is 0700, the file 0600."""
    root = paths.backups_dir()
    root.parent.mkdir(parents=True, exist_ok=True)
    for directory in reversed([backup.parent, *backup.parent.parents]):
        if directory.is_relative_to(root):
            directory.mkdir(exist_ok=True)
            os.chmod(directory, 0o700)
    atomic_write(backup, source.read_bytes(), 0o600)


def apply_writes(writes: list[PlannedWrite], manifest: Manifest, stamp: str | None = None) -> list[Path]:
    """Apply changed writes, taking a backup the first time the relay touches each path.

    The manifest is saved before and after every write, so a crash leaves it accurate.
    """
    stamp = stamp or timestamp()
    applied: list[Path] = []
    for write in writes:
        if not write.changed():
            continue
        key = str(write.path)
        entry = manifest.entries.get(key)
        if entry is None:
            if write.path.is_file():
                backup = _backup_target(write.path, stamp)
                _private_backup(write.path, backup)
                entry = ManifestEntry(
                    action="modified", backup=str(backup), mode=write.path.stat().st_mode & 0o777
                )
            else:
                entry = ManifestEntry(action="created", created_dirs=_missing_dirs(write.path))
        # The digest goes in first: a crash before the write leaves a mismatch, which only ever errs safe.
        manifest.entries[key] = entry.model_copy(update={"written_sha256": sha256(write.content)})
        manifest.save()
        atomic_write(write.path, write.content, write.mode)
        applied.append(write.path)
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
        manifest.entries[str(move.source)] = ManifestEntry(action="moved", moved_to=str(move.destination))
        manifest.save()
        shutil.move(str(move.source), str(move.destination))
        done.append(move.source)
    return done


def apply_links(links: list[PlannedLink], manifest: Manifest) -> list[Path]:
    blocked = [link.path for link in links if link.blocked()]
    if blocked:
        raise LinkBlockedError(blocked[0])
    done: list[Path] = []
    for link in links:
        if not link.changed():
            continue
        key = str(link.path)
        if key not in manifest.entries:
            manifest.entries[key] = ManifestEntry(action="created", created_dirs=_missing_dirs(link.path))
            manifest.save()
        link.path.parent.mkdir(parents=True, exist_ok=True)
        if link.path.is_symlink():
            link.path.unlink()
        link.path.symlink_to(link.target)
        done.append(link.path)
    return done


def _unmerged(target: Path, current: bytes, unmergers: Mapping[str, Unmerge]) -> bytes | None:
    unmerge = unmergers.get(str(target))
    if unmerge is None:
        return None
    try:
        return unmerge(current)
    except ValueError:
        return None


def decide(
    key: str, entry: ManifestEntry, unmergers: Mapping[str, Unmerge] | None = None
) -> tuple[Fate, bytes | None]:
    """What uninstall will do with one path, and the bytes to write when that is an unmerge."""
    target = Path(key)
    if entry.action == "moved":
        return "move back", None
    current = target.read_bytes() if target.is_file() else None
    unchanged = current is not None and entry.written_sha256 == sha256(current)
    backup = Path(entry.backup) if entry.backup else None
    if entry.action == "created":
        if current is None or entry.written_sha256 is None or unchanged:
            return "remove", None
    elif backup is not None and backup.is_file():
        if current is None or unchanged or sha256(backup.read_bytes()) == sha256(current):
            return "restore", None
    if current is not None:
        merged = _unmerged(target, current, unmergers or {})
        if merged is not None:
            return "unmerge", merged
    return "leave", None


@dataclass
class RestoreReport:
    restored: list[Path] = field(default_factory=list)
    unmerged: list[Path] = field(default_factory=list)
    left: list[Path] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    kept_backups: set[Path] = field(default_factory=set)

    @property
    def changed(self) -> list[Path]:
        return [*self.restored, *self.unmerged]


def restore(
    manifest: Manifest, only: set[str] | None = None, unmergers: Mapping[str, Unmerge] | None = None
) -> RestoreReport:
    """Undo recorded changes without discarding later edits.

    Created files are removed; a file the relay merged into loses only the relay's entries; a
    backup is restored only over a file the relay last wrote, byte for byte. Anything else stays.
    """
    report = RestoreReport()
    created_dirs: set[Path] = set()
    for key, entry in list(manifest.entries.items()):
        if only is not None and key not in only:
            continue
        target = Path(key)
        fate, merged = decide(key, entry, unmergers)
        if entry.action == "created":
            created_dirs.update(Path(d) for d in entry.created_dirs)
        if fate == "remove":
            if target.is_file() or target.is_symlink():
                target.unlink()
            report.restored.append(target)
        elif fate == "restore" and entry.backup:
            atomic_write(
                target, Path(entry.backup).read_bytes(), entry.mode if entry.mode is not None else 0o644
            )
            report.restored.append(target)
        elif fate == "unmerge" and merged is not None:
            atomic_write(target, merged, target.stat().st_mode & 0o777)
            report.unmerged.append(target)
        elif fate == "move back":
            if entry.moved_to and Path(entry.moved_to).exists() and not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(entry.moved_to, str(target))
            report.restored.append(target)
        else:
            report.left.append(target)
            if entry.backup:
                report.kept_backups.add(Path(entry.backup))
                report.notes.append(
                    f"{target}: left in place, changed since install; backup at {entry.backup}"
                )
            else:
                report.notes.append(f"{target}: left in place, changed since install")
        del manifest.entries[key]
        manifest.save()
    for directory in sorted(created_dirs, key=lambda d: len(d.parts), reverse=True):
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()
    return report


def render_json(value: object) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode()
