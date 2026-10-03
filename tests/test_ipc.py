"""AC-7: the socket is 0600 and the daemon refuses other uids."""

from __future__ import annotations

import asyncio
import os
import stat

import pytest

from agent_relay import ipc, paths


async def _echo(frame: ipc.JsonObj) -> ipc.JsonObj:
    return {"echo": frame}


async def test_roundtrip_and_mode() -> None:
    server = ipc.IpcServer(paths.socket_path(), _echo)
    await server.start()
    mode = stat.S_IMODE(os.stat(paths.socket_path()).st_mode)
    assert mode == 0o600
    assert stat.S_IMODE(os.stat(paths.app_support()).st_mode) == 0o700
    reply = await ipc.async_request({"op": "ping", "n": 1}, paths.socket_path(), timeout=2)
    assert reply == {"echo": {"op": "ping", "n": 1}}
    sync_reply = await asyncio.to_thread(ipc.request, {"op": "sync"}, paths.socket_path(), 2.0)
    assert sync_reply == {"echo": {"op": "sync"}}
    await server.stop()
    assert not paths.socket_path().exists()


async def test_refuses_other_uid() -> None:
    server = ipc.IpcServer(paths.socket_path(), _echo, expected_uid=os.getuid() + 1)
    await server.start()
    with pytest.raises((ConnectionError, asyncio.IncompleteReadError)):
        await ipc.async_request({"op": "ping"}, paths.socket_path(), timeout=2)
    assert server.refused == 1
    await server.stop()


def test_peer_uid_is_ours() -> None:
    import socket

    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        assert ipc.peer_uid(a) == os.getuid()
    finally:
        a.close()
        b.close()


def test_frame_limits() -> None:
    with pytest.raises(ipc.FrameError):
        ipc.decode_body(b"[1, 2]")
    assert ipc.encode({"a": 1})[:4] == (7).to_bytes(4, "big")


async def test_stream_ops_take_over_the_connection() -> None:
    seen: list[ipc.JsonObj] = []

    async def stream(frame: ipc.JsonObj, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        seen.append(frame)
        await ipc.write_frame(writer, {"ok": True})
        while (more := await ipc.read_frame(reader)) is not None:
            await ipc.write_frame(writer, {"streamed": more})

    server = ipc.IpcServer(paths.socket_path(), _echo, streams={"hold": stream})
    await server.start()
    reader, writer = await asyncio.open_unix_connection(str(paths.socket_path()))
    await ipc.write_frame(writer, {"op": "hold"})
    assert await ipc.read_frame(reader) == {"ok": True}
    await ipc.write_frame(writer, {"op": "ping"})
    assert await ipc.read_frame(reader) == {"streamed": {"op": "ping"}}
    assert seen == [{"op": "hold"}]
    assert await ipc.async_request({"op": "ping"}, paths.socket_path(), timeout=2) == {"echo": {"op": "ping"}}
    await asyncio.wait_for(server.stop(), 2)
    assert await ipc.read_frame(reader) is None
    writer.close()
