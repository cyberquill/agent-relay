"""The guided end-to-end smoke test (C15): phone steps by a human, Mac-side checks by relayd's IPC.

For each app: start a session, watch it stream, approve one tool, park it with `/away`, reply in the
thread, and release it with `/back`. Registration, parking and release are checked against relayd;
what only the phone shows (streaming, the approval card, the reply) is confirmed by the human.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from agent_relay.ipc import JsonObj
from agent_relay.models.events import Harness
from agent_relay.scrub import scrub_text

type Ipc = Callable[[JsonObj], JsonObj]
type Ask = Callable[[str], str]
type Sleep = Callable[[float], None]

WORKSPACE = "scratch"
PROMPT = "Run the shell command `echo relay-smoke`, then reply with exactly: OK"


@dataclass(frozen=True)
class SmokeApp:
    harness: Harness
    label: str
    start: str


APPS: tuple[SmokeApp, ...] = (
    SmokeApp(Harness.VSCODE_LOCAL, "VS Code Local", f'Phone: /new local {WORKSPACE} "{PROMPT}"'),
    SmokeApp(
        Harness.VSCODE_COPILOT, "VS Code Copilot harness", f'Phone: /new copilot {WORKSPACE} "{PROMPT}"'
    ),
    SmokeApp(Harness.OPENCODE, "OpenCode", f'Phone: /new opencode {WORKSPACE} "{PROMPT}"'),
    SmokeApp(
        Harness.CURSOR,
        "Cursor IDE (hooks and parking)",
        f"Mac: open .scratch in Cursor and send this in a new agent chat: {PROMPT}",
    ),
    SmokeApp(Harness.CURSOR_CLI, "Cursor CLI", f'Phone: /new cursor {WORKSPACE} "{PROMPT}"'),
)


@dataclass(frozen=True)
class Result:
    app: str
    step: str
    ok: bool
    evidence: str


@dataclass
class Session:
    key: str
    status: str


def sessions(ipc: Ipc, harness: Harness) -> dict[str, Session]:
    reply = ipc({"op": "sessions"})
    raw = reply.get("sessions")
    found: dict[str, Session] = {}
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict) and item.get("harness") == harness.value:
            key, status = item.get("session_key"), item.get("status")
            if isinstance(key, str) and isinstance(status, str):
                found[key] = Session(key, status)
    return found


@dataclass
class Waiter:
    ipc: Ipc
    timeout_s: float = 300.0
    poll_s: float = 2.0
    sleep: Sleep = time.sleep
    clock: Callable[[], float] = time.monotonic

    def until(self, harness: Harness, test: Callable[[dict[str, Session]], str | None]) -> str | None:
        """Poll relayd until `test` returns a session key, or give up at the timeout."""
        deadline = self.clock() + self.timeout_s
        while True:
            hit = test(sessions(self.ipc, harness))
            if hit is not None or self.clock() >= deadline:
                return hit
            self.sleep(self.poll_s)


@dataclass
class Smoke:
    waiter: Waiter
    ask: Ask
    say: Callable[[str], None]
    results: list[Result] = field(default_factory=list)

    def _record(self, app: SmokeApp, step: str, ok: bool, evidence: str) -> bool:
        self.results.append(Result(app.label, step, ok, scrub_text(evidence)))
        self.say(f"  {'PASS' if ok else 'FAIL'} {step}: {evidence}")
        return ok

    def _confirm(self, app: SmokeApp, step: str, question: str) -> bool:
        answer = self.ask(f"  {question} [y/n] ").strip().lower()
        return self._record(app, step, answer in {"y", "yes"}, f"human: {answer or 'no answer'}")

    def run_app(self, app: SmokeApp) -> bool:
        self.say(f"\n== {app.label} ==\n  {app.start}")
        before = set(sessions(self.waiter.ipc, app.harness))
        key = self.waiter.until(app.harness, lambda now: next((k for k in now if k not in before), None))
        if key is None:
            return self._record(app, "register", False, f"no new {app.harness.value} session appeared")
        self._record(app, "register", True, key)
        steps: tuple[Callable[[], bool], ...] = (
            lambda: self._confirm(app, "stream", "Did the thread show the reply streaming in?"),
            lambda: self._confirm(
                app, "approve", "Did an approval card arrive, and did Allow run the command?"
            ),
            lambda: self._parked(app, key),
            lambda: self._confirm(app, "reply", "Did your reply in the thread reach the parked agent?"),
            lambda: self._released(app, key),
        )
        return all(step() for step in steps)

    def _parked(self, app: SmokeApp, key: str) -> bool:
        self.say("  Phone: send /away, then a new prompt in the thread; wait for 'parked'.")
        hit = self.waiter.until(
            app.harness, lambda now: key if now.get(key, Session(key, "")).status == "parked" else None
        )
        return self._record(app, "park", hit is not None, f"{key} parked" if hit else "never parked")

    def _released(self, app: SmokeApp, key: str) -> bool:
        self.say("  Phone: send /back in the thread.")
        hit = self.waiter.until(
            app.harness, lambda now: key if now.get(key, Session(key, "ended")).status != "parked" else None
        )
        return self._record(app, "back", hit is not None, f"{key} released" if hit else "still parked")


def render(results: Sequence[Result], when: str) -> str:
    lines = [f"### Live smoke {when}", "", "| App | Step | Result | Evidence |", "|---|---|---|---|"]
    lines += [f"| {r.app} | {r.step} | {'✅' if r.ok else '❌'} | {r.evidence} |" for r in results]
    return "\n".join(lines) + "\n"
