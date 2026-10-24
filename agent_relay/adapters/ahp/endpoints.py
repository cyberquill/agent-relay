"""Find the editor's Agent Host socket with `code agent endpoints`.

The output carries the connection token, so it is parsed in memory and never logged. Its
`protocolVersion` is the newest version the host speaks, not the only one: the host also accepts older
compatible versions, so the version is settled by `initialize`, not here.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from agent_relay import paths
from agent_relay.adapters.ahp.models import PROTOCOL_VERSION
from agent_relay.adapters.base import AdapterUnavailableError
from agent_relay.scrub import scrub_text

SUPPORTED_VERSIONS: tuple[str, ...] = (PROTOCOL_VERSION,)
UNSUPPORTED_PROTOCOL_VERSION = -32005
CLI_TIMEOUT_S = 20.0

type Runner = Callable[[Sequence[str]], Awaitable[tuple[int, str, str]]]


class AhpUnavailableError(AdapterUnavailableError):
    """VS Code is closed, has no Agent Host, or speaks a protocol version the relay does not."""


class ProtocolMismatchError(AhpUnavailableError):
    """VS Code runs an Agent Host, but `initialize` found no protocol version both sides speak."""


def mismatch_message(advertised: str, selected: str | None = None) -> str:
    relay = ", ".join(SUPPORTED_VERSIONS)
    if selected is not None:
        return f"VS Code (AHP {advertised}) picked AHP {selected}; the relay supports {relay}"
    return f"VS Code (AHP {advertised}) refused AHP {relay}, the versions the relay supports"


@dataclass(frozen=True)
class Endpoint:
    socket_path: str
    protocol_version: str
    token: str = field(repr=False)
    pid: int | None = None


def _candidates(document: object) -> list[dict[str, object]]:
    if isinstance(document, dict):
        listed = document.get("endpoints")
        if isinstance(listed, list):
            return [e for e in listed if isinstance(e, dict)]
        if "endpoint" in document:
            return [document]
    if isinstance(document, list):
        return [e for e in document if isinstance(e, dict)]
    return []


def parse_endpoints(raw: str) -> Endpoint:
    try:
        document: object = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AhpUnavailableError("`code agent endpoints` did not print JSON") from exc
    for entry in _candidates(document):
        target = entry.get("endpoint")
        if not isinstance(target, dict) or target.get("type") != "socket":
            continue
        socket_path, token = target.get("path"), entry.get("connectionToken")
        version = entry.get("protocolVersion")
        if not isinstance(socket_path, str) or not isinstance(token, str) or not isinstance(version, str):
            continue
        pid = entry.get("pid")
        return Endpoint(socket_path, version, token, pid if isinstance(pid, int) else None)
    raise AhpUnavailableError("VS Code is not running an Agent Host (open VS Code)")


async def run_cli(argv: Sequence[str]) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), CLI_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "", "timed out"
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


async def discover(cli: Path = paths.VSCODE_CLI, runner: Runner = run_cli) -> Endpoint:
    if not cli.exists():
        raise AhpUnavailableError(f"VS Code CLI not found at {cli}")
    code, out, err = await runner([str(cli), "agent", "endpoints"])
    if code != 0:
        raise AhpUnavailableError(f"`code agent endpoints` failed ({code}): {scrub_text(err.strip())[:200]}")
    return parse_endpoints(out)
