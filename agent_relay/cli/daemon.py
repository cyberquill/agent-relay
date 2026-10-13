"""`relay start|stop|status|away`: drive the running relayd through launchd and its Unix socket."""

from __future__ import annotations

import json
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


def keep_alive_agents() -> list[launchd.LaunchAgent]:
    """The launch agents that keep running (relayd, then the OpenCode server); not the weekly update."""
    return [a for a in launchd.agents("python") if a.keep_alive]


def _start_agent(agent: launchd.LaunchAgent, runner: Runner) -> None:
    target = launchd.service_target(agent)
    if not launchd.is_loaded(agent, runner):
        code, output = runner(launchd.bootstrap_command(agent))
        if code != 0:
            raise fail(f"launchctl bootstrap failed ({code}): {output[:300]}")
    code, output = runner(["launchctl", "kickstart", "-k", target])
    if code != 0:
        raise fail(f"launchctl kickstart failed ({code}): {output[:300]}")


def start_daemon(runner: Runner = run) -> str:
    agent = relayd_agent()
    if not agent.path.is_file():
        raise fail(
            "relayd is not installed as a launch agent. Run `relay install --apply`, "
            "or run `python -m agent_relay.daemon` in a terminal."
        )
    started = [a for a in keep_alive_agents() if a is agent or a.path.is_file()]
    for each in started:
        _start_agent(each, runner)
    return f"started {', '.join(a.label for a in started)}; logs in {paths.logs_dir()}"


def stop_daemon(runner: Runner = run, sleep: Callable[[float], None] = time.sleep) -> str:
    """Boot every keep-alive agent out and wait until launchd lets go, so an immediate `start` bootstraps."""
    stopped: list[str] = []
    refusal = ""
    for agent in keep_alive_agents():
        if agent.name != "relayd" and not agent.path.is_file():
            continue
        code, output = runner(launchd.bootout_command(agent))
        if code != 0:
            if agent.name == "relayd":
                refusal = f"relayd is not loaded in launchd ({code}): {output[:200]}"
            continue
        stopped.append(agent.label)
        if not launchd.wait_unloaded(agent, runner, sleep):
            note(f"launchd still lists {agent.label}; give it a moment before `relay start`")
    if not stopped:
        raise fail(refusal)
    return f"stopped {', '.join(stopped)} (launchd restarts them at next login, or with `relay start`)"


def start() -> None:
    """Start (or restart) relayd and the OpenCode server under launchd."""
    out(start_daemon())


def stop() -> None:
    """Stop relayd and the OpenCode server."""
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
