"""relayd's end of relay-bridge: one long-lived connection per VS Code window.

The extension opens relay.sock and sends `{op: "bridge.register", window_id, folders, ...}`. relayd
answers `{ok: true, protocol}` and keeps the connection: commands go out as `{id, cmd, args}`,
replies come back as `{id, ok, result | error}`, and the window pushes `{event, ...}` frames.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from pydantic import JsonValue

from agent_relay import paths
from agent_relay.adapters.base import AdapterUnavailableError
from agent_relay.ipc import FrameError, JsonObj, StreamHandler, read_frame, write_frame
from agent_relay.log import get_logger

log = get_logger(__name__)

PROTOCOL = 1
REGISTER_OP = "bridge.register"
CALL_TIMEOUT_S = 10.0


class BridgeGoneError(AdapterUnavailableError):
    """The window closed, or did not answer in time."""


class BridgeCommandError(RuntimeError):
    """The extension ran the command and reported a failure."""


def normalize(raw: str) -> Path:
    return paths.expand_user(raw).resolve()


@dataclass(frozen=True)
class BridgeEvent:
    window_id: str
    name: str
    data: JsonObj


@dataclass
class BridgeWindow:
    window_id: str
    folders: tuple[Path, ...]
    version: str
    vscode_version: str
    writer: asyncio.StreamWriter
    timeout: float = CALL_TIMEOUT_S
    pending: dict[int, asyncio.Future[JsonValue]] = field(default_factory=dict)
    _ids: itertools.count[int] = field(default_factory=lambda: itertools.count(1))

    def match(self, workspace: str) -> int:
        """How specifically this window holds `workspace`: the matching folder's depth, or -1."""
        target = normalize(workspace)
        depths = [len(f.parts) for f in self.folders if target == f or f in target.parents]
        return max(depths, default=-1)

    async def call(self, cmd: str, args: JsonObj | None = None, timeout: float | None = None) -> JsonValue:
        if self.writer.is_closing():
            raise BridgeGoneError(f"VS Code window {self.window_id} is gone")
        call_id = next(self._ids)
        future: asyncio.Future[JsonValue] = asyncio.get_running_loop().create_future()
        self.pending[call_id] = future
        try:
            await write_frame(self.writer, {"id": call_id, "cmd": cmd, "args": dict(args or {})})
            return await asyncio.wait_for(future, timeout or self.timeout)
        except TimeoutError as exc:
            raise BridgeGoneError(f"VS Code window {self.window_id} did not answer `{cmd}`") from exc
        except ConnectionError as exc:
            raise BridgeGoneError(f"VS Code window {self.window_id} is gone") from exc
        finally:
            self.pending.pop(call_id, None)

    def resolve(self, frame: JsonObj) -> None:
        call_id = frame.get("id")
        future = self.pending.get(call_id) if isinstance(call_id, int) else None
        if future is None or future.done():
            return
        if frame.get("ok") is True:
            future.set_result(cast(JsonValue, frame.get("result")))
        else:
            future.set_exception(BridgeCommandError(str(frame.get("error") or "command failed")))

    def fail_all(self) -> None:
        for future in self.pending.values():
            if not future.done():
                future.set_exception(BridgeGoneError(f"VS Code window {self.window_id} disconnected"))
        self.pending.clear()


def _str_list(value: object) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


class BridgeHub:
    """Registry of connected VS Code windows. Shared by the IPC server and the vscode_local adapter."""

    def __init__(self, call_timeout: float = CALL_TIMEOUT_S, max_events: int = 256) -> None:
        self.call_timeout = call_timeout
        self.windows: dict[str, BridgeWindow] = {}
        self.events: asyncio.Queue[BridgeEvent] = asyncio.Queue(maxsize=max_events)
        self._changed = asyncio.Condition()

    def streams(self) -> dict[str, StreamHandler]:
        return {REGISTER_OP: self.serve}

    def window_for(self, workspace: str) -> BridgeWindow | None:
        """The window whose folder holds `workspace` most specifically (latest registered on a tie)."""
        best: tuple[int, BridgeWindow] | None = None
        for window in self.windows.values():
            depth = window.match(workspace)
            if depth >= 0 and (best is None or depth >= best[0]):
                best = (depth, window)
        return best[1] if best else None

    def any_window(self) -> BridgeWindow | None:
        return next(reversed(self.windows.values()), None)

    def window_holding(self, folders: Sequence[Path]) -> BridgeWindow | None:
        """The latest registered window that has every one of `folders` open (a multi-root workspace)."""
        wanted = {normalize(str(f)) for f in folders}
        held = [w for w in self.windows.values() if wanted <= set(w.folders)]
        return held[-1] if held else None

    async def wait_for(
        self, predicate: Callable[[], BridgeWindow | None], timeout: float
    ) -> BridgeWindow | None:
        async with self._changed:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._changed.wait_for(lambda: predicate() is not None), timeout)
            return predicate()

    async def _notify(self) -> None:
        async with self._changed:
            self._changed.notify_all()

    def _emit(self, event: BridgeEvent) -> None:
        if self.events.full():
            self.events.get_nowait()
        self.events.put_nowait(event)

    async def serve(self, frame: JsonObj, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        window_id = frame.get("window_id")
        if not isinstance(window_id, str) or not window_id:
            await write_frame(writer, {"ok": False, "error": "bridge.register needs a window_id"})
            return
        window = BridgeWindow(
            window_id=window_id,
            folders=tuple(normalize(f) for f in _str_list(frame.get("folders"))),
            version=str(frame.get("version") or ""),
            vscode_version=str(frame.get("vscode_version") or ""),
            writer=writer,
            timeout=self.call_timeout,
        )
        previous = self.windows.pop(window_id, None)
        if previous is not None:
            previous.fail_all()
            previous.writer.close()
        self.windows[window_id] = window
        await write_frame(writer, {"ok": True, "protocol": PROTOCOL})
        log.info(f"relay-bridge window {window_id} registered with {len(window.folders)} folder(s)")
        await self._notify()
        try:
            await self._read(window, reader)
        finally:
            window.fail_all()
            if self.windows.get(window_id) is window:
                del self.windows[window_id]
                log.info(f"relay-bridge window {window_id} disconnected")
            await self._notify()

    async def _read(self, window: BridgeWindow, reader: asyncio.StreamReader) -> None:
        while True:
            try:
                frame = await read_frame(reader)
            except (ConnectionError, FrameError, ValueError):
                return
            if frame is None:
                return
            if "id" in frame:
                window.resolve(frame)
                continue
            name = frame.get("event")
            if not isinstance(name, str):
                continue
            if name == "folders":
                window.folders = tuple(normalize(f) for f in _str_list(frame.get("folders")))
                await self._notify()
            self._emit(BridgeEvent(window.window_id, name, frame))
