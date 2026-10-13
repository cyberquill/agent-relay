"""fileops: private backups, atomic writes, a current manifest, and an uninstall that keeps later edits."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agent_relay import fileops, paths
from agent_relay.fileops import (
    Manifest,
    ManifestEntry,
    PlannedWrite,
    apply_writes,
    atomic_write,
    restore,
    sha256,
)


def _file(name: str, content: bytes = b"original relay\n") -> Path:
    target = paths.home() / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return target


def _install(target: Path, content: bytes = b"relay merged\n") -> Manifest:
    manifest = Manifest.load()
    apply_writes([PlannedWrite(path=target, content=content, mode=0o600)], manifest)
    return Manifest.load()


def test_atomic_write_never_exposes_a_wider_mode_even_before_the_chmod(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[int] = []
    replace = os.replace

    def spy(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
        seen.append(Path(source).stat().st_mode & 0o777)
        replace(source, destination)

    old = os.umask(0)
    try:
        monkeypatch.setattr(os, "fchmod", lambda fd, mode: None)
        monkeypatch.setattr(os, "replace", spy)
        atomic_write(tmp_path / "secret.json", b"{}", 0o600)
    finally:
        os.umask(old)
    assert seen == [0o600]


def test_atomic_write_sets_the_requested_mode_whatever_the_umask(tmp_path: Path) -> None:
    old = os.umask(0o077)
    try:
        atomic_write(tmp_path / "shared.txt", b"x", 0o644)
    finally:
        os.umask(old)
    assert (tmp_path / "shared.txt").stat().st_mode & 0o777 == 0o644


def test_atomic_write_writes_through_a_symlink_and_keeps_it(tmp_path: Path) -> None:
    real = tmp_path / "dotfiles" / "mcp.json"
    real.parent.mkdir()
    real.write_text("old")
    link = tmp_path / "mcp.json"
    link.symlink_to(real)
    atomic_write(link, b"new", 0o600)
    assert link.is_symlink() and link.readlink() == real
    assert real.read_bytes() == b"new"
    assert not list(tmp_path.rglob("*.relay-tmp"))


def test_atomic_write_removes_its_temp_file_when_the_write_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def refuse(source: object, destination: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(OSError, match="disk full"):
        atomic_write(tmp_path / "a.json", b"{}", 0o600)
    assert list(tmp_path.iterdir()) == []


def test_backups_are_private_and_the_backup_tree_is_0700() -> None:
    target = _file("deep/dir/mcp.json", b'{"token": "plaintext"}')
    paths.backups_dir().parent.mkdir(parents=True, exist_ok=True)
    old = os.umask(0)
    try:
        _install(target)
    finally:
        os.umask(old)
    backup = Path(Manifest.load().entries[str(target)].backup or "")
    assert backup.read_bytes() == b'{"token": "plaintext"}'
    assert backup.stat().st_mode & 0o777 == 0o600
    folder = backup.parent
    while folder != paths.backups_dir().parent:
        assert folder.stat().st_mode & 0o777 == 0o700, folder
        folder = folder.parent


def test_the_manifest_is_saved_after_every_write_so_a_crash_leaves_it_accurate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _file("one.txt")
    second = _file("two.txt")
    real = fileops.atomic_write

    def crash_on_second(path: Path, content: bytes, mode: int) -> None:
        if path == second:
            raise RuntimeError("power cut")
        real(path, content, mode)

    monkeypatch.setattr(fileops, "atomic_write", crash_on_second)
    writes = [PlannedWrite(path=p, content=b"relay merged\n") for p in (first, second)]
    with pytest.raises(RuntimeError, match="power cut"):
        apply_writes(writes, Manifest.load())
    entries = Manifest.load().entries
    assert entries[str(first)].written_sha256 == sha256(b"relay merged\n")
    assert str(second) in entries, "the entry is recorded before the write that crashed"


def test_uninstall_restores_a_file_nobody_has_touched_since() -> None:
    target = _file("a.json")
    manifest = _install(target)
    report = restore(manifest)
    assert target.read_bytes() == b"original relay\n"
    assert report.restored == [target] and report.left == []
    assert Manifest.load().entries == {}


def test_uninstall_leaves_a_changed_file_that_has_no_unmerger_and_keeps_its_backup() -> None:
    target = _file("b.json")
    manifest = _install(target)
    target.write_bytes(b"relay merged\nplus my own edit\n")
    backup = manifest.entries[str(target)].backup
    report = restore(manifest)
    assert target.read_bytes() == b"relay merged\nplus my own edit\n"
    assert report.left == [target] and report.kept_backups == {Path(backup or "")}
    assert report.notes == [f"{target}: left in place, changed since install; backup at {backup}"]
    assert Path(backup or "").is_file()


def test_uninstall_unmerges_a_changed_file_instead_of_restoring_the_backup() -> None:
    target = _file("c.json", b'{"servers": {"mine": 1}}')
    manifest = _install(target, b'{"servers": {"mine": 1, "relay": 2}}')
    target.write_bytes(b'{"servers": {"mine": 1, "relay": 2, "later": 3}}')

    def drop_relay(content: bytes) -> bytes:
        data = json.loads(content)
        data["servers"].pop("relay")
        return json.dumps(data).encode()

    report = restore(manifest, unmergers={str(target): drop_relay})
    assert json.loads(target.read_bytes()) == {"servers": {"mine": 1, "later": 3}}
    assert report.unmerged == [target] and report.left == []


def test_an_unparsable_file_is_treated_as_having_no_unmerger() -> None:
    target = _file("d.json")
    manifest = _install(target)
    target.write_bytes(b"{ this is not json")

    def parse(content: bytes) -> bytes:
        return json.dumps(json.loads(content)).encode()

    report = restore(manifest, unmergers={str(target): parse})
    assert target.read_bytes() == b"{ this is not json"
    assert report.left == [target]


def test_a_file_the_relay_created_is_removed_when_unchanged_and_kept_when_edited() -> None:
    untouched = paths.home() / "new" / "untouched.txt"
    edited = paths.home() / "new" / "edited.txt"
    manifest = Manifest.load()
    apply_writes([PlannedWrite(path=p, content=b"relay\n") for p in (untouched, edited)], manifest)
    edited.write_bytes(b"relay\nmine\n")
    report = restore(Manifest.load())
    assert not untouched.exists()
    assert edited.read_bytes() == b"relay\nmine\n"
    assert report.left == [edited] and "no backup" not in report.notes[0]
    assert edited.parent.exists(), "a folder that still holds a kept file is not removed"


def test_a_manifest_entry_without_a_digest_loads_and_never_overwrites_a_modified_file() -> None:
    target = _file("legacy.json")
    manifest = _install(target)
    entry = manifest.entries[str(target)]
    manifest.entries[str(target)] = ManifestEntry(action="modified", backup=entry.backup, mode=entry.mode)
    target.write_bytes(b"edited later\n")
    report = restore(manifest)
    assert target.read_bytes() == b"edited later\n" and report.left == [target]


def test_a_second_write_updates_the_digest_but_keeps_the_first_backup() -> None:
    target = _file("e.json")
    first = _install(target, b"v1 relay\n")
    backup = first.entries[str(target)].backup
    second = _install(target, b"v2 relay\n")
    assert second.entries[str(target)].backup == backup
    assert second.entries[str(target)].written_sha256 == sha256(b"v2 relay\n")
    restore(second)
    assert target.read_bytes() == b"original relay\n"
