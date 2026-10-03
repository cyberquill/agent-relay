"""A stand-in for the relay-bridge VS Code extension: registers a window over relay.sock and answers
relayd's commands the way the real extension does."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

from pydantic import JsonValue

from agent_relay.ipc import JsonObj, read_frame, write_frame

DEFAULT_MODELS: list[JsonValue] = [
    {
        "id": "claude-sonnet",
        "label": "Claude Sonnet",
        "vendor": "copilot",
        "family": "claude",
        "picker": True,
    },
    {"id": "gpt-5", "label": "GPT-5", "vendor": "copilot", "family": "gpt", "picker": True},
]


class FakeBridge:
    def __init__(
        self, sock_path: Path, window_id: str = "win-1", folders: tuple[str, ...] = ("/w/proj",)
    ) -> None:
        self.sock_path = sock_path
        self.window_id = window_id
        self.folders = list(folders)
        self.calls: list[tuple[str, JsonObj]] = []
        self.results: dict[str, JsonValue] = {"models": DEFAULT_MODELS}
        self.errors: dict[str, str] = {}
        self.silent: set[str] = set()
        self.registered: JsonObj | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task[None] | None = None

    async def connect(self, register: JsonObj | None = None) -> FakeBridge:
        reader, writer = await asyncio.open_unix_connection(str(self.sock_path))
        self._writer = writer
        frame: JsonObj = register or {
            "op": "bridge.register",
            "protocol": 1,
            "window_id": self.window_id,
            "folders": list(self.folders),
            "version": "0.1.0",
            "vscode_version": "1.140.0",
        }
        await write_frame(writer, frame)
        self.registered = await read_frame(reader)
        self._task = asyncio.create_task(self._serve(reader, writer))
        return self

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while True:
            frame = await read_frame(reader)
            if frame is None:
                return
            call_id, cmd, args = frame.get("id"), frame.get("cmd"), frame.get("args")
            if not isinstance(cmd, str):
                continue
            self.calls.append((cmd, dict(args) if isinstance(args, dict) else {}))
            if cmd in self.silent:
                continue
            if cmd in self.errors:
                await write_frame(writer, {"id": call_id, "ok": False, "error": self.errors[cmd]})
            else:
                result = self.results.get(cmd, {"done": True})
                await write_frame(writer, {"id": call_id, "ok": True, "result": result})

    async def push(self, event: JsonObj) -> None:
        assert self._writer is not None
        await write_frame(self._writer, event)

    def commands(self) -> list[str]:
        return [cmd for cmd, _ in self.calls]

    def args_of(self, cmd: str) -> JsonObj:
        return next(args for name, args in self.calls if name == cmd)

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(Exception):
                await self._writer.wait_closed()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
