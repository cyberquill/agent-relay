"""The phone's files on the Mac: where they are written, how long they wait, and when they are deleted.

Files live under `<workspace>/<inbox_dir>/<folder>/`, owner-only, never outside the workspace.
A files-only message is held in the registry until the text it belongs to arrives.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import mimetypes
import os
import re
import secrets
import shutil
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, ValidationError

from agent_relay import paths
from agent_relay.core.registry import Registry
from agent_relay.log import get_logger
from agent_relay.models.attachments import Attachment, Received
from agent_relay.models.config import AttachmentsConfig
from agent_relay.models.events import utcnow
from agent_relay.models.sessions import SessionStatus

log = get_logger(__name__)

HOLD_PREFIX = "attach.hold:"
DIRS_PREFIX = "attach.dirs:"
TOP = "top"
ENDED_GRACE = timedelta(days=1)
STAGING_DIR = "inbox-staging"
NEW_PREFIX = "new-"
CHUNK = 64 * 1024
MAX_STEM = 80
MAX_SUFFIX = 16
MAX_SAME_NAME = 1000
MB = 1024 * 1024
OCTET_STREAM = "application/octet-stream"

_UNSAFE = re.compile(r"[^A-Za-z0-9._ -]")


class SkipFileError(RuntimeError):
    """One file could not be taken. `reason` is what the user is told."""

    def __init__(self, name: str, reason: str) -> None:
        super().__init__(f"{name}: {reason}")
        self.name = name
        self.reason = reason

    def describe(self) -> str:
        return f"{self.name} ({self.reason})"


class TooLargeError(SkipFileError):
    pass


@dataclass
class Incoming:
    """A file on its way in: its claimed name and type, and the bytes."""

    name: str
    mime: str | None
    chunks: AsyncIterator[bytes]


class Held(BaseModel):
    model_config = ConfigDict(frozen=True)

    expires: datetime
    received: Received
    staged: bool = False


def human_size(size: int) -> str:
    if size >= MB:
        return f"{round(size / MB, 1):g} MB"
    return f"{max(round(size / 1024), 1)} KB"


def safe_name(raw: str) -> str:
    """A file name that is one plain path component: ASCII, no leading dot, bounded length."""
    base = raw.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = " ".join(_UNSAFE.sub("_", base).split()).strip(" .")
    cleaned = cleaned.lstrip(".")
    stem, suffix = os.path.splitext(cleaned)
    cleaned = f"{stem[:MAX_STEM]}{suffix[:MAX_SUFFIX]}".strip(" .")
    return cleaned or "file"


def folder_name(session_key: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", session_key).lstrip(".") or "session"


def settled_mime(declared: str | None, name: str) -> str:
    head = (declared or "").split(";", 1)[0].strip().lower()
    if head and head != OCTET_STREAM:
        return head
    return mimetypes.guess_type(name)[0] or OCTET_STREAM


def receipt_line(received: Received, native: bool) -> str:
    """After delivery: which files the app got as attachments, which as a path, which were left out."""
    how = "attached" if native else "as a path"
    parts = [f"{a.name} ({how})" for a in received.attachments]
    line = ", ".join(parts)
    if received.skipped:
        skipped = f"skipped {', '.join(received.skipped)}"
        line = f"{line}; {skipped}" if line else skipped[:1].upper() + skipped[1:]
    return f"📎 {line}"


def _inbox_parts(inbox_dir: str) -> tuple[str, ...]:
    posix = PurePosixPath(inbox_dir)
    parts = tuple(p for p in posix.parts if p != ".")
    if posix.is_absolute() or not parts or ".." in parts:
        raise SkipFileError("attachments", "`attachments.inbox_dir` must be a folder inside the workspace")
    return parts


def _exclude_file(workspace: Path) -> Path | None:
    git = workspace / ".git"
    if git.is_dir():
        return git / "info" / "exclude"
    if not git.is_file():
        return None
    found = re.match(r"gitdir:\s*(.+)", git.read_text(errors="replace").strip())
    if not found:
        return None
    gitdir = Path(found.group(1).strip())
    gitdir = gitdir if gitdir.is_absolute() else workspace / gitdir
    common = gitdir / "commondir"
    if common.is_file():
        gitdir = (gitdir / common.read_text().strip()).resolve()
    return gitdir / "info" / "exclude"


def _ignore_in_git(workspace: Path, top: str) -> None:
    """Keep the inbox out of `git status` without touching the project's own .gitignore."""
    try:
        target = _exclude_file(workspace)
        if target is None:
            return
        entry = f"{top}/"
        existing = target.read_text() if target.exists() else ""
        if entry in {line.strip() for line in existing.splitlines()}:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        lead = "" if not existing or existing.endswith("\n") else "\n"
        with target.open("a") as handle:
            handle.write(f"{lead}{entry}\n")
    except OSError as exc:
        log.warning(f"could not add `{top}/` to .git/info/exclude: {exc}")


def _walk(root: Path, parts: tuple[str, ...]) -> Path:
    """Create `parts` under `root` one real directory at a time (0700); a link anywhere is refused."""
    base = root.resolve()
    current = base
    for part in parts:
        nxt = current / part
        if nxt.is_symlink():
            raise SkipFileError(part, "the inbox folder is a link")
        if not nxt.exists():
            nxt.mkdir(mode=0o700)
            nxt.chmod(0o700)
        elif not nxt.is_dir():
            raise SkipFileError(part, "the inbox path is not a folder")
        current = nxt
    resolved = current.resolve()
    if not resolved.is_relative_to(base):
        raise SkipFileError(parts[-1], "the inbox folder leaves the workspace")
    return resolved


def _create(directory: Path, name: str) -> tuple[int, Path]:
    """Exclusively create a 0600 file, numbering the name when it is taken."""
    stem, suffix = os.path.splitext(name)
    for n in range(1, MAX_SAME_NAME + 1):
        candidate = name if n == 1 else f"{stem}-{n}{suffix}"
        target = directory / candidate
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            continue
        os.fchmod(fd, 0o600)
        return fd, target
    raise SkipFileError(name, "too many files with that name")


async def _file_chunks(file: Path) -> AsyncIterator[bytes]:
    with file.open("rb") as handle:
        while chunk := await asyncio.to_thread(handle.read, CHUNK):
            yield chunk


def _remove_files(attachments: tuple[Attachment, ...]) -> None:
    for item in attachments:
        target = Path(item.local_path)
        with contextlib.suppress(OSError):
            target.unlink(missing_ok=True)
        if target.parent.name.startswith(NEW_PREFIX) or target.parent.parent.name == STAGING_DIR:
            with contextlib.suppress(OSError):
                target.parent.rmdir()


def _parse_held(raw: str) -> Held | None:
    try:
        return Held.model_validate_json(raw)
    except ValidationError:
        return None


class Inbox:
    def __init__(self, registry: Registry, config: AttachmentsConfig) -> None:
        self._registry = registry
        self._config = config

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    @property
    def max_bytes(self) -> int:
        return self._config.max_bytes

    def _staging(self) -> Path:
        return paths.app_support() / STAGING_DIR

    # saving

    def _directory(self, workspace: Path | None, folder: str) -> Path:
        if workspace is None:
            root = self._staging()
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            return _walk(root, (folder,))
        if not workspace.is_dir():
            raise SkipFileError("attachments", "the session's workspace folder is missing")
        parts = _inbox_parts(self._config.inbox_dir)
        resolved = _walk(workspace, (*parts, folder))
        _ignore_in_git(workspace.resolve(), parts[0])
        return resolved

    async def store(
        self,
        incoming: Incoming,
        *,
        workspace: Path | None,
        folder: str,
        session_key: str | None = None,
    ) -> Attachment:
        """Write one file, refusing past `max_bytes`. Staging (no workspace yet) is the registry's home."""
        name = safe_name(incoming.name)
        directory = await asyncio.to_thread(self._directory, workspace, folder_name(folder))
        fd, target = await asyncio.to_thread(_create, directory, name)
        digest = hashlib.sha256()
        total = 0
        try:
            with os.fdopen(fd, "wb") as handle:
                async for chunk in incoming.chunks:
                    total += len(chunk)
                    if total > self._config.max_bytes:
                        raise TooLargeError(name, f"over {human_size(self._config.max_bytes)}")
                    digest.update(chunk)
                    await asyncio.to_thread(handle.write, chunk)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        if session_key is not None:
            await self._record_dir(session_key, directory)
        return Attachment(
            name=target.name,
            mime=settled_mime(incoming.mime, name),
            size=total,
            local_path=str(target),
            sha256=digest.hexdigest(),
        )

    async def _record_dir(self, session_key: str, directory: Path) -> None:
        key = f"{DIRS_PREFIX}{session_key}"
        raw = await self._registry.kv_get(key)
        known: list[str] = json.loads(raw) if raw else []
        if str(directory) not in known:
            await self._registry.kv_set(key, json.dumps([*known, str(directory)]))

    async def claim(self, session_key: str, received: Received) -> None:
        """Tie files saved before the session existed to it, so they go when it does."""
        for folder in {Path(a.local_path).parent for a in received.attachments}:
            await self._record_dir(session_key, folder)

    async def discard(self, received: Received) -> None:
        await asyncio.to_thread(_remove_files, received.attachments)

    # holding

    async def hold(self, key: str, received: Received, staged: bool = False) -> None:
        """Keep files that came without text until the next text in `key` (a thread id, or `top`)."""
        prior = await self.take(key)
        merged = prior.merged(received) if prior else received
        expires = utcnow() + timedelta(seconds=self._config.hold_s)
        held = Held(expires=expires, received=merged, staged=staged)
        await self._registry.kv_set(f"{HOLD_PREFIX}{key}", held.model_dump_json())

    async def take(self, key: str) -> Received | None:
        """The held files for `key`, now released. Files held past their time are deleted instead."""
        raw = await self._registry.kv_pop(f"{HOLD_PREFIX}{key}")
        held = _parse_held(raw) if raw else None
        if held is None:
            return None
        if held.expires <= utcnow():
            await asyncio.to_thread(_remove_files, held.received.attachments)
            return None
        return held.received

    async def take_for_new(self, workspace: Path) -> Received | None:
        """Files sent with `/new`, moved from staging into the new session's inbox folder."""
        held = await self.take(TOP)
        if held is None:
            return None
        folder = f"{NEW_PREFIX}{secrets.token_hex(4)}"
        saved: list[Attachment] = []
        skipped = list(held.skipped)
        for item in held.attachments:
            incoming = Incoming(item.name, item.mime, _file_chunks(Path(item.local_path)))
            try:
                saved.append(await self.store(incoming, workspace=workspace, folder=folder))
            except SkipFileError as exc:
                skipped.append(exc.describe())
        await asyncio.to_thread(_remove_files, held.attachments)
        return Received(attachments=tuple(saved), skipped=tuple(skipped))


def _remove_dir(path: Path, session_key: str) -> None:
    if path.name != folder_name(session_key) and not path.name.startswith(NEW_PREFIX):
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)


async def sweep_inbox(registry: Registry) -> int:
    """Delete held files past their time, and the folders of sessions gone or ended over a day ago."""
    now = utcnow()
    removed = 0
    for key, raw in (await registry.kv_prefix(HOLD_PREFIX)).items():
        held = _parse_held(raw)
        if held is not None and held.expires > now:
            continue
        if held is not None:
            await asyncio.to_thread(_remove_files, held.received.attachments)
        await registry.kv_delete(key)
        removed += 1
    recorded = await registry.kv_prefix(DIRS_PREFIX)
    if not recorded:
        return removed
    sessions = {s.session_key: s for s in await registry.list_sessions(include_ended=True)}
    for key, raw in recorded.items():
        session_key = key.removeprefix(DIRS_PREFIX)
        session = sessions.get(session_key)
        if session is not None and not (
            session.status is SessionStatus.ENDED and session.updated < now - ENDED_GRACE
        ):
            continue
        try:
            folders = [Path(p) for p in json.loads(raw)]
        except (ValueError, TypeError):
            folders = []
        for folder in folders:
            await asyncio.to_thread(_remove_dir, folder, session_key)
        await registry.kv_delete(key)
        removed += 1
        log.info(f"removed the phone files of {session_key}")
    return removed
