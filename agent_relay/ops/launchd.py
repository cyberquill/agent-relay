"""launchd agents: relayd, opencode serve, and the weekly Cursor CLI update. Rendered, never hand-written."""

from __future__ import annotations

import os
import plistlib
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from agent_relay import paths
from agent_relay.fileops import PlannedWrite

LABEL_PREFIX = "io.github.cyberquill.agent-relay"
SEARCH_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
WEEKLY_UPDATE = {"Weekday": 0, "Hour": 3, "Minute": 17}
UNLOAD_POLLS = 40
UNLOAD_POLL_S = 0.25

type PlistValue = str | int | bool | list[str] | dict[str, str] | dict[str, int]


@dataclass(frozen=True)
class LaunchAgent:
    name: str
    arguments: tuple[str, ...]
    keep_alive: bool = False
    run_at_load: bool = False
    calendar: dict[str, int] | None = None
    env: dict[str, str] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"{LABEL_PREFIX}.{self.name}"

    @property
    def path(self) -> Path:
        return paths.launch_agents_dir() / f"{self.label}.plist"

    def as_dict(self) -> dict[str, PlistValue]:
        logs = paths.logs_dir()
        body: dict[str, PlistValue] = {
            "Label": self.label,
            "ProgramArguments": list(self.arguments),
            "WorkingDirectory": str(paths.home()),
            "EnvironmentVariables": {"PATH": SEARCH_PATH, **self.env},
            "StandardOutPath": str(logs / f"{self.name}.out.log"),
            "StandardErrorPath": str(logs / f"{self.name}.err.log"),
            "ProcessType": "Background" if self.calendar else "Interactive",
        }
        if self.keep_alive:
            body["KeepAlive"] = True
            body["ThrottleInterval"] = 10
        if self.run_at_load:
            body["RunAtLoad"] = True
        if self.calendar:
            body["StartCalendarInterval"] = dict(self.calendar)
        return body

    def render(self) -> bytes:
        return plistlib.dumps(self.as_dict(), sort_keys=True)


def agents(python: str) -> list[LaunchAgent]:
    return [
        LaunchAgent("relayd", (python, "-m", "agent_relay.daemon"), keep_alive=True, run_at_load=True),
        LaunchAgent(
            "opencode",
            (python, "-m", "agent_relay.ops.launchers", "opencode"),
            keep_alive=True,
            run_at_load=True,
        ),
        LaunchAgent("cursor-update", (str(paths.cursor_agent_cli()), "update"), calendar=WEEKLY_UPDATE),
    ]


def plan(python: str) -> list[PlannedWrite]:
    return [
        PlannedWrite(
            path=agent.path, content=agent.render(), mode=0o644, reason=f"launchd agent {agent.label}"
        )
        for agent in agents(python)
    ]


def bootstrap_command(agent: LaunchAgent, uid: int | None = None) -> list[str]:
    return ["launchctl", "bootstrap", f"gui/{os.getuid() if uid is None else uid}", str(agent.path)]


def bootout_command(agent: LaunchAgent, uid: int | None = None) -> list[str]:
    return ["launchctl", "bootout", f"gui/{os.getuid() if uid is None else uid}/{agent.label}"]


def wait_unloaded(
    agent: LaunchAgent,
    runner: Callable[[Sequence[str]], tuple[int, str]],
    sleep: Callable[[float], None] = time.sleep,
    polls: int = UNLOAD_POLLS,
) -> bool:
    """Wait until launchd no longer lists `agent`. `bootout` returns before it lets go, and a
    `bootstrap` in that window fails with error 5 (or a `kickstart` with 37)."""
    target = f"gui/{os.getuid()}/{agent.label}"
    for _ in range(polls):
        if runner(["launchctl", "print", target])[0] != 0:
            return True
        sleep(UNLOAD_POLL_S)
    return False
