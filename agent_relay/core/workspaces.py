"""Registering workspaces and repos: folder checks, clone URL rules, `git clone`, `.code-workspace` edits.

Everything that touches the filesystem or git for `relay ws` and the phone's `/ws` lives here. Callers
never hand this module a path to trust: folders are re-checked against the configured roots, and a
clone URL must match one of two fixed shapes on an allowed host.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import signal
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import JsonValue

from agent_relay import paths
from agent_relay.fileops import atomic_write
from agent_relay.models.config import Workspace, WorkspacesConfig
from agent_relay.scrub import scrub_text
from agent_relay.settings import CODE_WORKSPACE_SUFFIX, code_workspace_folders
from agent_relay.sync import jsonc

NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,40}")
MAX_FOLDERS = 25
MAX_SCANNED = 5_000
ERROR_LINES = 5
ERROR_CHARS = 600
SSH_HINT = (
    "SSH keys may not be visible to the relay (it runs under launchd), so an `https://` URL with a "
    "Keychain-stored credential is the more reliable choice."
)

_SEGMENT = r"[A-Za-z0-9._-]+"
_HOST = r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?"
_HTTPS = re.compile(rf"https://(?P<host>{_HOST})/(?P<owner>{_SEGMENT})/(?P<repo>{_SEGMENT}?)(?:\.git)?")
_SSH = re.compile(rf"git@(?P<host>{_HOST}):(?P<owner>{_SEGMENT})/(?P<repo>{_SEGMENT}?)(?:\.git)?")


class WorkspaceError(ValueError):
    """A user-facing refusal: the message is shown as is."""


class CloneError(WorkspaceError):
    """`git clone` did not produce a checkout."""


@dataclass(frozen=True)
class CloneSpec:
    url: str
    host: str
    owner: str
    repo: str


@dataclass(frozen=True)
class FolderChoice:
    path: Path
    label: str
    git: bool


@dataclass(frozen=True)
class FolderOffer:
    """The folders a picker shows, and how many qualified in all."""

    choices: tuple[FolderChoice, ...]
    total: int


# names


def validate_name(name: str, taken: Iterable[str]) -> str:
    if not NAME_RE.fullmatch(name):
        raise WorkspaceError("A workspace name is 1 to 40 characters from letters, digits, `.`, `_` and `-`.")
    if name in set(taken):
        raise WorkspaceError(f"a workspace named {name!r} exists; remove it first with `relay ws rm {name}`")
    return name


# registering a folder or a `.code-workspace` file


def workspace_entry(name: str, folder: str) -> Workspace:
    """The registry entry for a folder, or for a multi-root file whose first folder becomes `path` (D60)."""
    resolved = paths.expand_user(folder).resolve()
    if resolved.is_file() and resolved.name.endswith(CODE_WORKSPACE_SUFFIX):
        first = code_workspace_folders(resolved)[0]
        if not first.is_dir():
            raise ValueError(f"{first}, the first folder of {resolved}, is not a directory")
        return Workspace(name=name, path=str(first), file=str(resolved))
    if not resolved.is_dir():
        raise ValueError(f"{resolved} is not a directory or a {CODE_WORKSPACE_SUFFIX} file")
    return Workspace(name=name, path=str(resolved))


def with_workspace(current: WorkspacesConfig, name: str, folder: str) -> WorkspacesConfig:
    validate_name(name, (w.name for w in current.workspaces))
    return WorkspacesConfig(workspaces=[*current.workspaces, workspace_entry(name, folder)])


# folders under the roots


def existing_roots(raw: Sequence[str]) -> list[Path]:
    """The configured roots that exist as folders, resolved and without repeats."""
    found: list[Path] = []
    for entry in raw:
        resolved = paths.expand_user(entry).resolve()
        if resolved.is_dir() and resolved not in found:
            found.append(resolved)
    return found


def _inside(candidate: Path, root: Path) -> bool:
    return candidate != root and root in candidate.parents


def _hidden(parts: Iterable[str]) -> bool:
    return any(part.startswith(".") for part in parts)


def _scan(root: Path, depth: int, exclude: Collection[Path]) -> list[FolderChoice]:
    found: list[FolderChoice] = []
    scanned = 0
    level = [root]
    for _ in range(depth):
        following: list[Path] = []
        for parent in level:
            try:
                with os.scandir(parent) as listing:
                    entries = sorted(listing, key=lambda e: e.name)
            except OSError:
                continue
            for entry in entries:
                scanned += 1
                if scanned > MAX_SCANNED:
                    return found
                if entry.name.startswith(".") or not entry.is_dir():
                    continue
                resolved = Path(entry.path).resolve()
                if not _inside(resolved, root):
                    continue
                git = (resolved / ".git").exists()
                if resolved not in exclude:
                    found.append(FolderChoice(resolved, _label(resolved, git), git))
                if not git:
                    following.append(resolved)
        level = following
    return found


def _label(folder: Path, git: bool) -> str:
    shown = paths.tilde(folder)
    return f"{shown} (git)" if git else shown


def _mtime(folder: Path) -> float:
    try:
        return folder.stat().st_mtime
    except OSError:
        return 0.0


def list_folders(
    roots: Sequence[Path], depth: int, exclude: Collection[Path] = (), limit: int = MAX_FOLDERS
) -> FolderOffer:
    """Folders under the roots, `depth` levels down. Git repos come first, then the most recently changed."""
    skip = {p.resolve() for p in exclude}
    unique: dict[Path, FolderChoice] = {}
    for root in roots:
        for choice in _scan(root, depth, skip):
            unique.setdefault(choice.path, choice)
    ranked = sorted(unique.values(), key=lambda c: (not c.git, -_mtime(c.path), str(c.path)))
    return FolderOffer(tuple(ranked[:limit]), len(ranked))


def check_folder(raw: object, roots: Sequence[Path], depth: int, exclude: Collection[Path] = ()) -> Path:
    """A folder the picker could have offered. Anything else, however it was typed, is refused."""
    if not isinstance(raw, str) or not raw or not Path(raw).is_absolute():
        raise WorkspaceError("Pick a folder from the list.")
    resolved = Path(raw).resolve()
    for root in roots:
        if not _inside(resolved, root):
            continue
        parts = resolved.relative_to(root).parts
        if len(parts) <= depth and not _hidden(parts) and resolved.is_dir():
            if resolved in {p.resolve() for p in exclude}:
                raise WorkspaceError("That folder is already registered.")
            return resolved
    raise WorkspaceError("That folder is not one the list offered.")


def check_root(raw: object, roots: Sequence[Path]) -> Path:
    if not isinstance(raw, str) or not raw:
        raise WorkspaceError("Pick a root from the list.")
    resolved = Path(raw).resolve()
    if resolved not in roots:
        raise WorkspaceError("That is not one of the configured roots.")
    return resolved


# cloning


def parse_clone_url(raw: object, hosts: Sequence[str]) -> CloneSpec:
    """`https://<host>/<owner>/<repo>[.git]` or `git@<host>:<owner>/<repo>[.git]`; nothing else."""
    url = raw.strip() if isinstance(raw, str) else ""
    match = _HTTPS.fullmatch(url) or _SSH.fullmatch(url)
    if match is None:
        raise WorkspaceError(
            "Use `https://<host>/<owner>/<repo>` or `git@<host>:<owner>/<repo>` (`.git` is optional)."
        )
    host, owner, repo = match["host"].lower(), match["owner"], match["repo"]
    if ".." in owner or ".." in repo or owner == "." or repo in (".", ".git"):
        raise WorkspaceError("That repository path is not allowed.")
    if host not in {h.lower() for h in hosts}:
        allowed = ", ".join(hosts) or "none"
        raise WorkspaceError(f"Cloning from `{host}` is not allowed. Allowed hosts: {allowed}.")
    return CloneSpec(url, host, owner, repo)


def clone_destination(root: Path, repo: str) -> Path:
    target = root / repo
    if os.path.lexists(target):
        raise WorkspaceError(f"{target} already exists. Add it as a folder instead.")
    if target.parent.resolve() != root.resolve():
        raise WorkspaceError("That repository name is not allowed.")
    return target


def git_env(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """The only environment `git clone` sees: no tokens, no shell state, never a prompt."""
    env = source if source is not None else os.environ
    kept = {key: env[key] for key in ("HOME", "PATH", "SSH_AUTH_SOCK") if key in env}
    kept["GIT_TERMINAL_PROMPT"] = "0"
    return kept


def _tail(output: bytes) -> str:
    lines = scrub_text(output.decode("utf-8", "replace")).strip().splitlines()
    return "\n".join(lines[-ERROR_LINES:])[-ERROR_CHARS:]


def _kill(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)


async def clone_repository(url: str, target: Path, timeout_s: float) -> None:
    """`git clone -- <url> <target>` with a timeout. A failure leaves nothing behind."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "clone",
            "--",
            url,
            str(target),
            env=git_env(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise CloneError("`git` is not installed or not on the relay's PATH.") from exc
    try:
        try:
            output, _ = await asyncio.wait_for(proc.communicate(), timeout_s)
        except TimeoutError as exc:
            raise CloneError(f"git clone took longer than {int(timeout_s)} seconds and was stopped.") from exc
        if proc.returncode != 0:
            raise CloneError(f"git clone failed:\n{_tail(output)}\n{SSH_HINT}")
    except BaseException:
        _kill(proc)
        with contextlib.suppress(Exception):
            await proc.wait()
        shutil.rmtree(target, ignore_errors=True)
        raise


# adding a repo to a workspace


def _reference(folder: Path, base: Path) -> str:
    """The `folders[].path` text for `folder`: relative when it is under the file's directory."""
    try:
        return str(folder.resolve().relative_to(base.resolve()))
    except ValueError:
        return str(folder.resolve())


def _new_file(ws: Workspace, roots: Sequence[Path]) -> Path:
    if not NAME_RE.fullmatch(ws.name):
        raise WorkspaceError(f"{ws.name!r} cannot name a {CODE_WORKSPACE_SUFFIX} file; rename the workspace.")
    home = paths.expand_user(ws.path).resolve()
    base = next((r for r in roots if r in home.parents), None) or home.parent
    return base / f"{ws.name}{CODE_WORKSPACE_SUFFIX}"


def workspace_folders(ws: Workspace) -> list[Path]:
    """Every folder a workspace holds: its `file`'s folders when it has one, else just `path`."""
    own = paths.expand_user(ws.path).resolve()
    if not ws.file:
        return [own]
    try:
        return [own, *code_workspace_folders(paths.expand_user(ws.file).resolve())]
    except ValueError:
        return [own]


def _read_document(file: Path) -> dict[str, JsonValue]:
    try:
        data = jsonc.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WorkspaceError(f"cannot read {file}: {exc}") from exc
    if not isinstance(data, dict):
        raise WorkspaceError(f"{file} is not a workspace file.")
    return data


def check_attachable(ws: Workspace, roots: Sequence[Path]) -> None:
    """Fail now, before anything is cloned, if the workspace file could not take another folder."""
    if ws.file:
        _read_document(paths.expand_user(ws.file).resolve())
        return
    new = _new_file(ws, roots)
    if new.exists():
        raise WorkspaceError(
            f"{new} already exists. Register that file instead: `relay ws rm {ws.name}`, "
            f"then `relay ws add {ws.name} {new}`."
        )


def attach_folder(ws: Workspace, folder: Path, roots: Sequence[Path]) -> Workspace:
    """Add `folder` to the workspace's `.code-workspace` file, creating the file for a plain folder.

    Returns the registry entry to save: `path` stays the first folder (D60), `file` names the file.
    Comments in an existing file are not kept; every other key is.
    """
    check_attachable(ws, roots)
    folder = folder.resolve()
    if folder in workspace_folders(ws):
        raise WorkspaceError(f"`{ws.name}` already has {folder}.")
    first = paths.expand_user(ws.path).resolve()
    if ws.file:
        target = paths.expand_user(ws.file).resolve()
        document = _read_document(target)
    else:
        target = _new_file(ws, roots)
        document = {"folders": [{"path": _reference(first, target.parent)}]}
    entries = document.get("folders")
    if not isinstance(entries, list):
        entries = []
    document["folders"] = [*entries, {"path": _reference(folder, target.parent)}]
    mode = target.stat().st_mode & 0o777 if target.exists() else 0o644
    atomic_write(target, jsonc.dumps(document).encode("utf-8"), mode)
    return Workspace(name=ws.name, path=ws.path, file=str(target))
