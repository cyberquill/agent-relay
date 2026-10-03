"""JSON-RPC 2.0 over a child process's stdio, one JSON message per line (the ACP transport)."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import cast

from pydantic import JsonValue

from agent_relay.adapters.base import AdapterUnavailableError
from agent_relay.log import get_logger
from agent_relay.scrub import scrub_text

log = get_logger(__name__)

type Params = dict[str, JsonValue]
type RequestHandler = Callable[[str, Params], Awaitable[JsonValue]]
type NotificationHandler = Callable[[str, Params], Awaitable[None]]

LINE_LIMIT = 16 * 1024 * 1024
METHOD_NOT_FOUND = -32601
INTERNAL_ERROR = -32603


class AcpExitedError(AdapterUnavailableError):
    """The agent process exited (or was never reachable)."""


class AcpRpcError(RuntimeError):
    def __init__(self, method: str, code: int, message: str) -> None:
        super().__init__(f"{method} failed ({code}): {message}")
        self.method = method
        self.code = code
        self.message = message


class MethodNotFoundError(RuntimeError):
    """Raised by a request handler for a method the client does not implement."""


class AcpProcess:
    def __init__(
        self,
        proc: asyncio.subprocess.Process,
        on_request: RequestHandler,
        on_notification: NotificationHandler,
        stderr_lines: int = 40,
    ) -> None:
        self._proc = proc
        self._on_request = on_request
        self._on_notification = on_notification
        self._ids = itertools.count(1)
        self._pending: dict[int, tuple[str, asyncio.Future[JsonValue]]] = {}
        self._write_lock = asyncio.Lock()
        self._handlers: set[asyncio.Task[None]] = set()
        self.stderr: deque[str] = deque(maxlen=stderr_lines)
        self._reader = asyncio.create_task(self._read())
        self._errors = asyncio.create_task(self._read_stderr())

    @classmethod
    async def spawn(
        cls,
        argv: Sequence[str],
        cwd: Path,
        on_request: RequestHandler,
        on_notification: NotificationHandler,
        env: dict[str, str] | None = None,
    ) -> AcpProcess:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd),
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=LINE_LIMIT,
            )
        except OSError as exc:
            raise AcpExitedError(f"could not start {Path(argv[0]).name}: {exc.strerror or exc}") from exc
        return cls(proc, on_request, on_notification)

    @property
    def alive(self) -> bool:
        return self._proc.returncode is None and not self._reader.done()

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode

    def last_error(self) -> str:
        return self.stderr[-1] if self.stderr else ""

    async def _send(self, message: dict[str, JsonValue]) -> None:
        stdin = self._proc.stdin
        if stdin is None or stdin.is_closing():
            raise AcpExitedError(self._exit_text())
        line = json.dumps(message, separators=(",", ":")) + "\n"
        async with self._write_lock:
            try:
                stdin.write(line.encode())
                await stdin.drain()
            except (ConnectionError, BrokenPipeError) as exc:
                raise AcpExitedError(self._exit_text()) from exc

    async def request(self, method: str, params: Params, timeout: float | None = None) -> JsonValue:
        call_id = next(self._ids)
        future: asyncio.Future[JsonValue] = asyncio.get_running_loop().create_future()
        self._pending[call_id] = (method, future)
        try:
            await self._send({"jsonrpc": "2.0", "id": call_id, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(call_id, None)

    async def notify(self, method: str, params: Params) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _exit_text(self) -> str:
        code = self._proc.returncode
        detail = f": {self.last_error()}" if self.last_error() else ""
        return (
            f"the agent process exited ({code}){detail}" if code is not None else f"the agent is gone{detail}"
        )

    async def _read(self) -> None:
        stdout = self._proc.stdout
        assert stdout is not None
        try:
            while line := await stdout.readline():
                try:
                    message = json.loads(line)
                except ValueError:
                    log.debug("ignoring a non-JSON line from the agent")
                    continue
                if isinstance(message, dict):
                    await self._route(cast(dict[str, JsonValue], message))
        except (ValueError, ConnectionError) as exc:
            log.warning(f"agent stdout closed: {exc}")
        finally:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._proc.wait(), 2)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(self._errors), 2)
            error = AcpExitedError(self._exit_text())
            for _, future in self._pending.values():
                if not future.done():
                    future.set_exception(error)

    async def _read_stderr(self) -> None:
        stderr = self._proc.stderr
        assert stderr is not None
        with contextlib.suppress(ValueError, ConnectionError):
            while line := await stderr.readline():
                text = scrub_text(line.decode(errors="replace").strip())
                if text:
                    self.stderr.append(text)

    async def _route(self, message: dict[str, JsonValue]) -> None:
        method = message.get("method")
        params = message.get("params")
        args: Params = params if isinstance(params, dict) else {}
        if isinstance(method, str) and "id" in message:
            task = asyncio.create_task(self._answer(message["id"], method, args))
            self._handlers.add(task)
            task.add_done_callback(self._handlers.discard)
            return
        if isinstance(method, str):
            try:
                await self._on_notification(method, args)
            except Exception:
                log.exception(f"handling the agent's {method} notification failed")
            return
        call_id = message.get("id")
        entry = self._pending.get(call_id) if isinstance(call_id, int) else None
        if entry is None or entry[1].done():
            return
        name, future = entry
        error = message.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            future.set_exception(
                AcpRpcError(name, code if isinstance(code, int) else 0, str(error.get("message") or ""))
            )
        else:
            future.set_result(message.get("result"))

    async def _answer(self, call_id: JsonValue, method: str, params: Params) -> None:
        reply: dict[str, JsonValue] = {"jsonrpc": "2.0", "id": call_id}
        try:
            reply["result"] = await self._on_request(method, params)
        except MethodNotFoundError:
            reply["error"] = {"code": METHOD_NOT_FOUND, "message": f"method not found: {method}"}
        except Exception as exc:
            log.exception(f"answering the agent's {method} request failed")
            reply["error"] = {"code": INTERNAL_ERROR, "message": str(exc)}
        with contextlib.suppress(AcpExitedError):
            await self._send(reply)

    async def close(self, timeout: float = 5.0) -> None:
        """Close stdin and wait; kill the process if it does not exit in time."""
        for task in list(self._handlers):
            task.cancel()
        stdin = self._proc.stdin
        if stdin is not None and not stdin.is_closing():
            stdin.close()
        if self._proc.returncode is None:
            try:
                await asyncio.wait_for(self._proc.wait(), timeout)
            except TimeoutError:
                self._proc.kill()
                await self._proc.wait()
        for task in (self._reader, self._errors):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(task, 2)
