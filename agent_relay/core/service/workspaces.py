"""Adding workspaces and repos: the folders on offer, registering one, cloning a repo, attaching it."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

from agent_relay.core import workspaces as ws
from agent_relay.core.service.common import Deps, RelayError
from agent_relay.core.workspaces import FolderOffer, WorkspaceError
from agent_relay.log import get_logger
from agent_relay.models.config import Workspace, WsConfig
from agent_relay.settings import load_workspaces, register_workspace, replace_workspace

log = get_logger(__name__)

type Started = Callable[[str], Awaitable[None]]

NOT_ENABLED = (
    "Adding workspaces from the phone is off. Enable it with "
    "`relay config set ws.roots '[\"<folder holding your repos>\"]'`, then `relay start`."
)


class WorkspaceAdmin:
    def __init__(self, deps: Deps) -> None:
        self._deps = deps
        self._lock = asyncio.Lock()

    @property
    def settings(self) -> WsConfig:
        return self._deps.config.ws

    def roots(self) -> list[Path]:
        return ws.existing_roots(self.settings.roots)

    def _require_roots(self) -> list[Path]:
        found = self.roots()
        if not found:
            raise RelayError(NOT_ENABLED)
        return found

    def _find(self, name: object) -> Workspace:
        for candidate in load_workspaces().workspaces:
            if candidate.name == name:
                return candidate
        raise RelayError(f"Unknown workspace `{name}`. Try `/ws`.")

    async def folders(self, workspace: str | None = None) -> FolderOffer:
        """What the folder picker shows: everything under the roots, minus what is already in use."""
        roots = self._require_roots()
        if workspace is None:
            taken = [Path(w.path) for w in load_workspaces().workspaces]
        else:
            taken = ws.workspace_folders(self._find(workspace))
        depth = self.settings.list_depth
        return await asyncio.to_thread(ws.list_folders, roots, depth, taken)

    def _folder(self, raw: object, exclude: list[Path]) -> Path:
        try:
            return ws.check_folder(raw, self._require_roots(), self.settings.list_depth, exclude)
        except WorkspaceError as exc:
            raise RelayError(str(exc)) from exc

    async def add_folder(self, folder: object, name: str | None) -> Workspace:
        async with self._lock:
            current = load_workspaces()
            chosen = self._folder(folder, [Path(w.path) for w in current.workspaces])
            try:
                wanted = ws.validate_name(name or chosen.name, [w.name for w in current.workspaces])
                entry = ws.workspace_entry(wanted, str(chosen))
                register_workspace(entry)
            except ValueError as exc:
                raise RelayError(str(exc)) from exc
            log.info(f"workspace {entry.name} added from the phone")
            return entry

    def _plan_clone(self, url: object, root: object) -> tuple[Path, ws.CloneSpec]:
        try:
            spec = ws.parse_clone_url(url, self.settings.clone_hosts)
            base = ws.check_root(root, self._require_roots())
            target = ws.clone_destination(base, spec.repo)
        except WorkspaceError as exc:
            raise RelayError(str(exc)) from exc
        return target, spec

    async def _run_clone(self, spec: ws.CloneSpec, target: Path, started: Started | None) -> None:
        if started is not None:
            await started(f"{spec.owner}/{spec.repo}")
        log.info(f"cloning {spec.host}/{spec.owner}/{spec.repo}")
        try:
            await ws.clone_repository(spec.url, target, self.settings.clone_timeout_s)
        except WorkspaceError as exc:
            raise RelayError(str(exc)) from exc

    async def clone(
        self, url: object, name: str | None, root: object, started: Started | None = None
    ) -> Workspace:
        async with self._lock:
            target, spec = self._plan_clone(url, root)
            try:
                wanted = ws.validate_name(name or spec.repo, [w.name for w in load_workspaces().workspaces])
            except WorkspaceError as exc:
                raise RelayError(str(exc)) from exc
            await self._run_clone(spec, target, started)
            entry = ws.workspace_entry(wanted, str(target))
            register_workspace(entry)
            return entry

    async def add_repo_folder(self, workspace: object, folder: object) -> Workspace:
        async with self._lock:
            current = self._find(workspace)
            chosen = self._folder(folder, ws.workspace_folders(current))
            return self._attach(current, chosen)

    async def add_repo_clone(
        self, workspace: object, url: object, root: object, started: Started | None = None
    ) -> Workspace:
        async with self._lock:
            current = self._find(workspace)
            target, spec = self._plan_clone(url, root)
            try:
                ws.check_attachable(current, self._require_roots())
            except WorkspaceError as exc:
                raise RelayError(str(exc)) from exc
            await self._run_clone(spec, target, started)
            try:
                return self._attach(current, target)
            except RelayError as exc:
                raise RelayError(
                    f"Cloned to {target}, but it was not added to `{current.name}`: {exc}"
                ) from exc

    def _attach(self, current: Workspace, folder: Path) -> Workspace:
        try:
            updated = ws.attach_folder(current, folder, self._require_roots())
        except WorkspaceError as exc:
            raise RelayError(str(exc)) from exc
        if updated != current:
            replace_workspace(updated)
        log.info(f"folder added to workspace {updated.name} from the phone")
        return updated
