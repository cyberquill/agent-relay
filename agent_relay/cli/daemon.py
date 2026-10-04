"""`relay start|stop|status|away`: drive the running relayd through launchd and its Unix socket."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from enum import StrEnum
from typing import Annotated

import typer

from agent_relay import paths
from agent_relay.cli.output import fail, note, out
from agent_relay.ipc import FrameError, JsonObj, request
from agent_relay.ops import launchd
from agent_relay.ops.install import Runner, run

IPC_TIMEOUT_S = 10.0
STOP_POLLS = 40
STOP_POLL_S = 0.25
NOT_RUNNING = "relayd is not running; start it with `relay start`"


class Switch(StrEnum):
    on = "on"
    off = "off"


def relayd_agent() -> launchd.LaunchAgent:
    return next(a for a in launchd.agents("python") if a.name == "relayd")


def ask(message: JsonObj) -> JsonObj:
    try:
        return request(message, paths.socket_path(), timeout=IPC_TIMEOUT_S)
    except (OSError, TimeoutError, FrameError) as exc:
        raise fail(NOT_RUNNING) from exc


def start_daemon(runner: Runner = run) -> str:
    agent = relayd_agent()
    if not agent.path.is_file():
        raise fail(
            "relayd is not installed as a launch agent. Run `relay install --apply`, "
            "or run `python -m agent_relay.daemon` in a terminal."
        )
    target = f"gui/{os.getuid()}/{agent.label}"
    code, _ = runner(["launchctl", "print", target])
    if code != 0:
        code, output = runner(launchd.bootstrap_command(agent))
        if code != 0:
            raise fail(f"launchctl bootstrap failed ({code}): {output[:300]}")
    code, output = runner(["launchctl", "kickstart", "-k", target])
    if code != 0:
        raise fail(f"launchctl kickstart failed ({code}): {output[:300]}")
    return f"started {agent.label}; logs in {paths.logs_dir()}"


def stop_daemon(runner: Runner = run, sleep: Callable[[float], None] = time.sleep) -> str:
    """Boot relayd out and wait until launchd has let go of it, so an immediate `start` bootstraps."""
    agent = relayd_agent()
    code, output = runner(launchd.bootout_command(agent))
    if code != 0:
        raise fail(f"relayd is not loaded in launchd ({code}): {output[:200]}")
    target = f"gui/{os.getuid()}/{agent.label}"
    for _ in range(STOP_POLLS):
        if runner(["launchctl", "print", target])[0] != 0:
            break
        sleep(STOP_POLL_S)
    else:
        note(f"launchd still lists {agent.label}; give it a moment before `relay start`")
    return f"stopped {agent.label} (launchd restarts it at next login, or with `relay start`)"


def start() -> None:
    """Start (or restart) relayd under launchd."""
    out(start_daemon())


def stop() -> None:
    """Stop relayd."""
    out(stop_daemon())


def status(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Sessions, parked turns, pending requests and adapter health from the running relayd."""
    reply = ask({"op": "status"})
    if as_json:
        out(json.dumps(reply, indent=2))
        return
    out(f"sessions {reply.get('sessions')}, parked {reply.get('parked')}, pending {reply.get('pending')}")
    adapters = reply.get("adapters")
    if isinstance(adapters, dict):
        for name, failures in sorted(adapters.items()):
            out(f"  {name}: {failures} recent failure(s)")
    if reply.get("paused"):
        note("relaying is paused (/panic); `relay away on` resumes")


def away(
    switch: Annotated[Switch, typer.Argument(help="on or off")],
    workspace: Annotated[
        str | None, typer.Option("--workspace", help="Limit to one workspace (path).")
    ] = None,
) -> None:
    """Turn away mode on or off, for every workspace or one."""
    scope = str(paths.expand_user(workspace).resolve()) if workspace else None
    message: JsonObj = {"op": "away", "enabled": switch is Switch.on}
    if scope is not None:
        message["workspace"] = scope
    reply = ask(message)
    if "error" in reply:
        raise fail(str(reply["error"]))
    out(f"away mode {'on' if reply.get('away') else 'off'} for {scope or 'all workspaces'}")
