"""Length-prefixed JSON frames over the relay's Unix socket. Stdlib-only: `relay-hook` imports it.

A frame is a 4-byte big-endian length followed by that many bytes of UTF-8 JSON (one object).
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import struct
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

MAX_FRAME = 16 * 1024 * 1024
DISCONNECT_POLL_S = 0.1
_HEADER = struct.Struct(">I")

type JsonObj = dict[str, object]
type Handler = Callable[[JsonObj], Awaitable[JsonObj | None]]
type StreamHandler = Callable[[JsonObj, asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


class FrameError(ValueError):
    pass


def encode(message: JsonObj) -> bytes:
    body = json.dumps(message, separators=(",", ":")).encode()
    if len(body) > MAX_FRAME:
        raise FrameError(f"frame too large: {len(body)} bytes")
    return _HEADER.pack(len(body)) + body


def decode_body(body: bytes) -> JsonObj:
    value = json.loads(body)
    if not isinstance(value, dict):
        raise FrameError("frame is not a JSON object")
    return value


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < n:
        chunk = sock.recv(n - len(chunks))
        if not chunk:
            raise ConnectionError("socket closed mid-frame")
        chunks.extend(chunk)
    return bytes(chunks)


def request(
    message: JsonObj, sock_path: Path, timeout: float | None, connect_timeout: float = 0.5
) -> JsonObj:
    """Blocking request/response used by `relay-hook` and `relay-mcp`."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(connect_timeout)
        sock.connect(str(sock_path))
        sock.settimeout(timeout)
        sock.sendall(encode(message))
        (length,) = _HEADER.unpack(_recv_exact(sock, _HEADER.size))
        if length > MAX_FRAME:
            raise FrameError(f"frame too large: {length} bytes")
        return decode_body(_recv_exact(sock, length))


async def read_frame(reader: asyncio.StreamReader) -> JsonObj | None:
    """The next frame, or None when the peer closed (also when it closed mid-frame)."""
    try:
        header = await reader.readexactly(_HEADER.size)
        (length,) = _HEADER.unpack(header)
        if length > MAX_FRAME:
            raise FrameError(f"frame too large: {length} bytes")
        return decode_body(await reader.readexactly(length))
    except asyncio.IncompleteReadError:
        return None


async def write_frame(writer: asyncio.StreamWriter, message: JsonObj) -> None:
    writer.write(encode(message))
    await writer.drain()


async def async_request(message: JsonObj, sock_path: Path, timeout: float | None = None) -> JsonObj:
    reader, writer = await asyncio.open_unix_connection(str(sock_path))
    try:
        await write_frame(writer, message)
        frame = await asyncio.wait_for(read_frame(reader), timeout)
        if frame is None:
            raise ConnectionError("server closed without a reply")
        return frame
    finally:
        writer.close()


_LOCAL_PEERCRED = 0x001
_SOL_LOCAL = 0
_IS_DARWIN: bool = sys.platform == "darwin"


def peer_uid(sock: socket.socket) -> int:
    """The uid of the process on the other end of a Unix socket."""
    if _IS_DARWIN:
        raw = sock.getsockopt(_SOL_LOCAL, _LOCAL_PEERCRED, 76)
        (_version, uid) = struct.unpack_from("=Ii", raw, 0)
        return int(uid)
    so_peercred: int = getattr(socket, "SO_PEERCRED", 17)
    raw = sock.getsockopt(socket.SOL_SOCKET, so_peercred, struct.calcsize("3i"))
    (_pid, uid, _gid) = struct.unpack("3i", raw)
    return int(uid)


class IpcServer:
    """Serves one request per frame; a connection may carry several. Refuses other uids.

    A frame whose `op` names a stream handler hands the whole connection to that handler, which
    owns it until it returns (used by long-lived peers such as the relay-bridge extension).
    """

    def __init__(
        self,
        sock_path: Path,
        handler: Handler,
        expected_uid: int | None = None,
        streams: dict[str, StreamHandler] | None = None,
    ) -> None:
        self.sock_path = sock_path
        self.handler = handler
        self.streams: dict[str, StreamHandler] = dict(streams or {})
        self.expected_uid = os.getuid() if expected_uid is None else expected_uid
        self.refused = 0
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self.sock_path.parent.mkdir(parents=True, exist_ok=True)
        self.sock_path.parent.chmod(0o700)
        if self.sock_path.exists() or self.sock_path.is_symlink():
            self.sock_path.unlink()
        old_umask = os.umask(0o177)
        try:
            self._server = await asyncio.start_unix_server(self._on_client, path=str(self.sock_path))
        finally:
            os.umask(old_umask)
        self.sock_path.chmod(0o600)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server.close_clients()
            await self._server.wait_closed()
            self._server = None
        if self.sock_path.exists():
            self.sock_path.unlink()

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        sock = writer.get_extra_info("socket")
        try:
            if sock is None or peer_uid(sock) != self.expected_uid:
                self.refused += 1
                return
            while True:
                frame = await read_frame(reader)
                if frame is None:
                    return
                stream = self.streams.get(str(frame.get("op")))
                if stream is not None:
                    await stream(frame, reader, writer)
                    return
                connected, reply = await self._serve(reader, frame)
                if not connected:
                    return
                if reply is not None:
                    await write_frame(writer, reply)
        except (ConnectionError, FrameError, json.JSONDecodeError):
            return
        finally:
            writer.close()

    async def _serve(self, reader: asyncio.StreamReader, frame: JsonObj) -> tuple[bool, JsonObj | None]:
        """Run the handler as a task and cancel it if the client hangs up first.

        A hook blocked on an approval or a park would otherwise keep waiting, and keep its phone
        card answerable, for a process that no longer exists. Returns (still connected, reply).
        """
        task = asyncio.ensure_future(self.handler(frame))
        try:
            while not task.done():
                await asyncio.wait({task}, timeout=DISCONNECT_POLL_S)
                if not task.done() and (reader.at_eof() or reader.exception() is not None):
                    return False, None
            return True, task.result()
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
