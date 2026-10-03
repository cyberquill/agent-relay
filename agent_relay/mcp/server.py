"""relay-mcp: a stdio MCP server that lets an agent reach the phone through relayd.

Tools: `notify_phone(text)` and `ask_phone(question, options, timeout_s<=900)`. The workspace is
`RELAY_WORKSPACE` when set, else the server's working directory (harnesses start MCP servers there).
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Literal, Protocol

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession
from pydantic import BaseModel

from agent_relay import ipc, paths
from agent_relay.log import configure, default_log_file, get_logger

log = get_logger(__name__)

ASK_MAX_S = 900
PROGRESS_EVERY_S = 20.0
NOTIFY_TIMEOUT_S = 30.0
INSTRUCTIONS = (
    "Reach the user's phone through Agent Relay. Use notify_phone for short status updates and "
    "ask_phone only for real blockers while the user is away; prefer deciding yourself."
)


class ProgressSink(Protocol):
    async def report_progress(
        self, progress: float, total: float | None = None, message: str | None = None
    ) -> None: ...


class NotifyResult(BaseModel):
    delivered: bool
    reason: str = ""


class AskResult(BaseModel):
    outcome: Literal["answered", "timeout", "denied"]
    answer: str | None = None
    reason: str = ""


def workspace() -> str:
    return os.environ.get("RELAY_WORKSPACE") or str(Path.cwd())


async def _call(message: dict[str, object], sock: Path, timeout: float) -> dict[str, object]:
    try:
        return await ipc.async_request(message, sock, timeout)
    except (OSError, ConnectionError) as exc:
        return {"error": f"relayd is not reachable ({exc.__class__.__name__}); start it with `relay start`"}
    except TimeoutError:
        return {"error": "relayd did not answer in time"}


async def notify(text: str, sock: Path, folder: str) -> NotifyResult:
    reply = await _call({"op": "notify", "text": text, "workspace": folder}, sock, NOTIFY_TIMEOUT_S)
    if "error" in reply:
        return NotifyResult(delivered=False, reason=str(reply["error"]))
    return NotifyResult(delivered=bool(reply.get("delivered")), reason=str(reply.get("reason") or ""))


async def ask(
    question: str,
    options: list[str],
    timeout_s: int,
    sock: Path,
    folder: str,
    ctx: ProgressSink | None = None,
) -> AskResult:
    timeout = min(max(int(timeout_s), 1), ASK_MAX_S)
    message: dict[str, object] = {
        "op": "ask",
        "question": question,
        "options": list(options),
        "timeout_s": timeout,
        "workspace": folder,
    }
    call = asyncio.create_task(_call(message, sock, timeout + NOTIFY_TIMEOUT_S))
    waited = 0.0
    while True:
        done, _ = await asyncio.wait({call}, timeout=PROGRESS_EVERY_S)
        if done:
            break
        waited += PROGRESS_EVERY_S
        if ctx is not None:
            await ctx.report_progress(min(waited, timeout), float(timeout), "waiting for the phone")
    reply = call.result()
    if "error" in reply:
        return AskResult(outcome="denied", reason=str(reply["error"]))
    outcome = reply.get("outcome")
    if outcome == "answered":
        answer = reply.get("answer")
        return AskResult(outcome="answered", answer=answer if isinstance(answer, str) else "")
    if outcome == "timeout":
        return AskResult(outcome="timeout", reason="no answer before the deadline")
    return AskResult(outcome="denied", reason=str(reply.get("reason") or ""))


def build_server(sock: Path | None = None, folder: str | None = None) -> FastMCP:
    server = FastMCP("agent-relay", instructions=INSTRUCTIONS)

    @server.tool()
    async def notify_phone(text: str) -> NotifyResult:
        """Post a short status line to the user's phone, in the thread of this session."""
        return await notify(text, sock or paths.socket_path(), folder or workspace())

    @server.tool()
    async def ask_phone(
        question: str,
        ctx: Context[ServerSession, object, object],
        options: list[str] | None = None,
        timeout_s: int = ASK_MAX_S,
    ) -> AskResult:
        """Ask the user on their phone and wait for the answer (at most 900 s).

        Returns outcome `answered` with the answer, `timeout`, or `denied` (away mode off, skipped,
        paused, or relayd not running). Use only for real blockers.
        """
        return await ask(
            question, options or [], timeout_s, sock or paths.socket_path(), folder or workspace(), ctx
        )

    return server


def main() -> None:
    configure(logging.INFO, default_log_file("relay-mcp"), console=False)
    log.info(f"relay-mcp serving workspace {workspace()}")
    build_server().run("stdio")


if __name__ == "__main__":
    main()
