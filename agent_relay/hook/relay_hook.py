"""`relay-hook`: the program every harness runs for its hooks. Stdlib-only, for startup time.

It forwards the hook payload to `relayd` and writes back exactly what `relayd` decides. If
`relayd` is down, has no opinion, or the payload is unreadable, it answers "no opinion": no output
for Copilot and OpenCode, and valid JSON for Cursor, whose permission hooks block on missing or
invalid output.

Usage: relay-hook --harness <copilot|cursor|opencode> --event <native event name>
"""

from __future__ import annotations

import json
import socket
import struct
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_relay import paths

_HEADER = struct.Struct(">I")
CONNECT_TIMEOUT_S = 0.2
CURSOR_PERMISSION_EVENTS = frozenset({"preToolUse", "beforeShellExecution", "beforeMCPExecution"})


def no_opinion(harness: str, event: str) -> str:
    """Cursor merges hook answers as deny > ask > allow, so `allow` defers to every other hook."""
    if harness != "cursor":
        return ""
    return '{"permission": "allow"}' if event in CURSOR_PERMISSION_EVENTS else "{}"


def _args(argv: list[str]) -> tuple[str, str] | None:
    harness = event = None
    it = iter(argv)
    for arg in it:
        if arg == "--harness":
            harness = next(it, None)
        elif arg == "--event":
            event = next(it, None)
    if not harness or not event:
        return None
    return harness, event


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("relayd closed the connection")
        buf.extend(chunk)
    return bytes(buf)


def exchange(message: dict[str, object], sock_path: Path) -> dict[str, object] | None:
    """Send one frame and wait (without a deadline: relayd owns the deadline) for the reply."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(CONNECT_TIMEOUT_S)
        try:
            sock.connect(str(sock_path))
        except OSError:
            return None
        sock.settimeout(None)
        body = json.dumps(message, separators=(",", ":")).encode()
        sock.sendall(_HEADER.pack(len(body)) + body)
        (length,) = _HEADER.unpack(_recv_exact(sock, _HEADER.size))
        reply = json.loads(_recv_exact(sock, length))
    return reply if isinstance(reply, dict) else None


def run(argv: list[str], stdin_text: str, sock_path: Path) -> tuple[int, str]:
    """Returns (exit code, stdout text)."""
    parsed = _args(argv)
    if parsed is None:
        return 0, ""
    harness, event = parsed
    passthrough = (0, no_opinion(harness, event))
    try:
        payload = json.loads(stdin_text) if stdin_text.strip() else {}
    except json.JSONDecodeError:
        return passthrough
    if not isinstance(payload, dict):
        return passthrough
    try:
        reply = exchange({"op": "hook", "harness": harness, "event": event, "payload": payload}, sock_path)
    except (OSError, ValueError, struct.error):
        return passthrough
    if reply is None:
        return passthrough
    out = reply.get("stdout")
    code = reply.get("exit", 0)
    if out is None or out == "":
        return (code if isinstance(code, int) else 0), passthrough[1]
    text = out if isinstance(out, str) else json.dumps(out)
    return (code if isinstance(code, int) else 0), text


def main() -> None:
    code, text = run(sys.argv[1:], sys.stdin.read(), paths.socket_path())
    if text:
        sys.stdout.write(text)
        sys.stdout.flush()
    sys.exit(code)


if __name__ == "__main__":
    main()
