"""A file an agent wants on the phone: checked against the workspace it belongs to, then scrubbed.

The path must resolve (symlinks followed first) to a regular file inside the session's workspace
and fit the attachment cap. Text is redacted like everything else the relay posts; other files go
through as they are.
"""

from __future__ import annotations

import mimetypes
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from agent_relay import paths
from agent_relay.core.inbox import OCTET_STREAM, human_size, safe_name
from agent_relay.scrub import scrub_text

TEXT_SUFFIXES = frozenset(
    {
        ".txt",
        ".md",
        ".markdown",
        ".json",
        ".jsonl",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".conf",
        ".env",
        ".csv",
        ".tsv",
        ".log",
        ".diff",
        ".patch",
        ".html",
        ".htm",
        ".xml",
        ".svg",
        ".css",
        ".js",
        ".ts",
        ".py",
        ".sh",
        ".rst",
        ".sql",
    }
)
TEXT_TYPES = ("text/", "application/json", "application/xml", "application/x-yaml", "application/toml")


class SendFileError(Exception):
    """The file cannot be sent. The message is safe to show the user and the agent."""


@dataclass(frozen=True)
class SendableFile:
    name: str
    data: bytes
    content_type: str


def _content_type(name: str) -> str:
    return mimetypes.guess_type(name)[0] or OCTET_STREAM


def _is_texty(name: str, content_type: str) -> bool:
    return content_type.startswith(TEXT_TYPES) or Path(name).suffix.lower() in TEXT_SUFFIXES


def _resolve(workspace: str, path: str) -> Path:
    if not path.strip():
        raise SendFileError("no file path was given")
    try:
        root = Path(workspace).resolve(strict=True)
    except OSError as exc:
        raise SendFileError("the session's workspace folder is missing") from exc
    requested = paths.expand_user(path)
    candidate = requested if requested.is_absolute() else root / requested
    try:
        target = candidate.resolve(strict=True)
    except OSError as exc:
        raise SendFileError(f"{path} does not exist") from exc
    if not target.is_relative_to(root):
        raise SendFileError(f"{path} is outside this session's workspace")
    return target


def _read(target: Path, shown: str, max_bytes: int) -> bytes:
    """Read a regular file without following a link swapped in after the check, up to the cap."""
    try:
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise SendFileError(f"{shown} cannot be read") from exc
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise SendFileError(f"{shown} is not a regular file")
        if info.st_size > max_bytes:
            raise SendFileError(
                f"{shown} is {human_size(info.st_size)}, over the {human_size(max_bytes)} limit"
            )
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise SendFileError(f"{shown} is over the {human_size(max_bytes)} limit")
    return data


def validate_file(workspace: str, path: str, max_bytes: int) -> SendableFile:
    """The file at `path` (relative to `workspace`, or absolute) if it may go to the phone."""
    target = _resolve(workspace, path)
    shown = target.name
    if not target.is_file():
        raise SendFileError(f"{shown} is not a regular file")
    data = _read(target, shown, max_bytes)
    name = safe_name(target.name)
    content_type = _content_type(name)
    if _is_texty(name, content_type):
        try:
            text = data.decode()
        except UnicodeDecodeError:
            return SendableFile(name, data, content_type)
        data = scrub_text(text).encode()
        if len(data) > max_bytes:
            raise SendFileError(f"{shown} is over the {human_size(max_bytes)} limit")
    return SendableFile(name, data, content_type)
