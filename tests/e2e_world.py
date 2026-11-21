"""A fake Webex room with a relay bot in it, for the phone E2E driver's offline tests.

`World` is both the `WebexUser` the driver talks to and the relay bot that answers. The bot behaves as the
plan's section 3 describes; each `Behaviour` flag makes it break one rule, so a test can show the matching
scenario fails. Time is virtual: `FakeClock.sleep` advances it and runs the bot's scheduled replies.
"""

from __future__ import annotations

import heapq
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import JsonValue

from scripts.e2e.model import Download, DriverError, Msg, Person, SubmitRefused

ROOM = "room-1"
BOT = "relay@webex.bot"
ME = "owner@example.com"
CARD_TYPE = "application/vnd.microsoft.card.adaptive"
WORKSPACE = "/Users/o/agent-relay/.scratch"
UPLOAD_URL = "https://webexapis.com/v1/contents/upload"
ANSWER_URL = "https://webexapis.com/v1/contents/answer"


@dataclass
class Behaviour:
    """Each flag breaks one rule of the target behaviour."""

    submit_scope: bool = True
    probe_lies: bool = False  # the scope probe says yes, the real submit is refused
    extra_final: bool = False  # a second message in the same turn
    chatter_top: bool = False  # a top-level "Turn complete" after the final
    approval_card: bool = False  # an Allow/Deny card during the shell turn
    card_kept: bool = False  # a one-shot card is not deleted after submit
    no_root: bool = False  # the session never opens a thread
    help_twice: bool = False
    script_in_html: bool = False
    href_in_html: bool = False
    no_csp: bool = False
    no_html_file: bool = False
    ignore_files: bool = False  # the agent does not name the attached files
    wrong_model_reply: bool = False
    single_model: bool = False
    no_model_card: bool = False
    desk_turn: bool = True
    back_keeps_parked: bool = False
    panic_keeps_running: bool = False
    stop_ignored: bool = False  # /stop is acknowledged but the turn keeps running
    final_text: str = "OK"
    final_delay: float = 5.0


class FakeClock:
    def __init__(self, world: World) -> None:
        self._world = world
        self.t = 0.0

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds
        self._world.run_due(self.t)


def _card(body: list[JsonValue], actions: list[JsonValue]) -> list[dict[str, JsonValue]]:
    content: dict[str, JsonValue] = {
        "type": "AdaptiveCard",
        "version": "1.3",
        "body": body,
        "actions": actions,
    }
    return [{"contentType": CARD_TYPE, "content": content}]


def _submit(title: str, data: dict[str, JsonValue]) -> JsonValue:
    return {"type": "Action.Submit", "title": title, "data": data}


def _choices(pairs: list[tuple[str, str]]) -> list[JsonValue]:
    return [{"title": title, "value": value} for title, value in pairs]


@dataclass
class _Raw:
    id: str
    person_email: str
    text: str
    parent_id: str | None
    created: float
    attachments: list[dict[str, JsonValue]] = field(default_factory=list)
    files: list[str] = field(default_factory=list)

    def msg(self) -> Msg:
        return Msg.model_validate(
            {
                "id": self.id,
                "roomId": ROOM,
                "personEmail": self.person_email,
                "text": self.text,
                "markdown": self.text,
                "parentId": self.parent_id,
                "files": self.files,
                "attachments": self.attachments,
                "created": f"{self.created:012.1f}",
            }
        )


class World:
    def __init__(self, behaviour: Behaviour | None = None) -> None:
        self.b = behaviour or Behaviour()
        self.clock = FakeClock(self)
        self.raw: dict[str, _Raw] = {}
        self.order: list[str] = []
        self.deleted: set[str] = set()
        self.cards: dict[str, str] = {}  # message id -> card kind
        self.roots: dict[str, dict[str, str]] = {}
        self.uploads: dict[str, str] = {}  # message id -> uploaded file name
        self.held: list[str] = []
        self.running_long: set[str] = set()
        self.submitted: list[tuple[str, dict[str, JsonValue]]] = []
        self.sent: list[str] = []
        self.paused = False
        self.desk_done = False
        self._queue: list[tuple[float, int, Callable[[], object]]] = []
        self._seq = 0

    # scheduling ------------------------------------------------------------
    def later(self, delay: float, fn: Callable[[], object]) -> None:
        self._seq += 1
        heapq.heappush(self._queue, (self.clock.t + delay, self._seq, fn))

    def run_due(self, now: float) -> None:
        while self._queue and self._queue[0][0] <= now:
            _, _, fn = heapq.heappop(self._queue)
            fn()

    # messages --------------------------------------------------------------
    def add(
        self,
        who: str,
        text: str,
        *,
        parent_id: str | None = None,
        attachments: list[dict[str, JsonValue]] | None = None,
        files: list[str] | None = None,
    ) -> _Raw:
        raw = _Raw(
            f"m{len(self.order) + 1}", who, text, parent_id, self.clock.t, attachments or [], files or []
        )
        self.raw[raw.id] = raw
        self.order.append(raw.id)
        return raw

    def bot(self, text: str, parent_id: str | None = None, files: list[str] | None = None) -> _Raw:
        return self.add(BOT, text, parent_id=parent_id, files=files)

    def bot_card(
        self, kind: str, text: str, body: list[JsonValue], actions: list[JsonValue], parent_id: str | None
    ) -> _Raw:
        raw = self.add(BOT, text, parent_id=parent_id, attachments=_card(body, actions))
        self.cards[raw.id] = kind
        return raw

    def refresh_root(self, root_id: str) -> None:
        info = self.roots[root_id]
        model = info["model"] or "default model"
        self.raw[
            root_id
        ].text = f"{info['emoji']} **{info['harness']} · scratch · {model} · {info['status']}**"

    def open_session(self, harness: str, *, parked: bool = False) -> None:
        if self.b.no_root:
            return
        root = self.add(BOT, "")
        self.roots[root.id] = {
            "harness": harness,
            "model": "",
            "emoji": "⬛" if parked else "🟦",
            "status": "🅿️ parked" if parked else "⏳ running",
        }
        self.refresh_root(root.id)
        self.later(self.b.final_delay, lambda: self.finish(root.id, self.b.final_text, idle=not parked))

    def finish(self, root_id: str, text: str, *, idle: bool = True, files: list[str] | None = None) -> None:
        self.bot(text, root_id, files)
        if self.b.extra_final:
            self.later(2.0, lambda: self.bot("one more thing", root_id))
        if self.b.chatter_top:
            self.later(1.0, lambda: self.bot("✅ Turn complete"))
        if idle and root_id in self.roots and "🅿️" not in self.roots[root_id]["status"]:
            self.roots[root_id]["status"] = "💤 idle"
            self.refresh_root(root_id)

    # the WebexUser side ----------------------------------------------------
    async def whoami(self) -> Person:
        return Person(id="me", email=ME)

    async def find_room(self, title: str) -> str:
        return ROOM

    async def counterpart(self, room_id: str) -> Person:
        return Person(id="bot", email=BOT)

    async def send(
        self, room_id: str, text: str, *, parent_id: str | None = None, file: Path | None = None
    ) -> Msg:
        raw = self.add(ME, text, parent_id=parent_id, files=[UPLOAD_URL] if file else [])
        if file is not None:
            self.uploads[raw.id] = file.name
        self.sent.append(text)
        self.react(raw)
        return raw.msg()

    async def list_messages(
        self, room_id: str, *, parent_id: str | None = None, limit: int = 50
    ) -> list[Msg]:
        ids = [i for i in reversed(self.order) if i not in self.deleted]
        picked = [self.raw[i] for i in ids if parent_id is None or self.raw[i].parent_id == parent_id]
        return [r.msg() for r in picked[:limit]]

    async def get(self, message_id: str) -> Msg | None:
        return (
            None if message_id in self.deleted or message_id not in self.raw else self.raw[message_id].msg()
        )

    async def delete(self, message_id: str) -> None:
        self.deleted.add(message_id)

    async def download(self, ref: str) -> Download:
        if ref != ANSWER_URL:
            raise DriverError("unknown file")
        meta = (
            ""
            if self.b.no_csp
            else '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'">'
        )
        extra = "<script>alert(1)</script>" if self.b.script_in_html else ""
        link = '<a href="https://example.com">x</a>' if self.b.href_in_html else ""
        html = f"<!doctype html><html><head>{meta}</head><body><table></table>{extra}{link}</body></html>"
        return Download("answer.html", "text/html", html.encode())

    async def can_submit_cards(self) -> bool:
        return self.b.submit_scope or self.b.probe_lies

    async def submit_card(self, message_id: str, inputs: dict[str, JsonValue]) -> None:
        if not self.b.submit_scope:
            raise SubmitRefused(403, "insufficient scope")
        self.submitted.append((message_id, inputs))
        kind = self.cards.get(message_id)
        if kind is None:
            raise DriverError("no such card")
        handler = {
            "new": self._new_submitted,
            "model": self._model_submitted,
            "panic": self._panic_submitted,
        }[kind]
        if not self.b.card_kept:
            self.deleted.add(message_id)
        handler(message_id, inputs)

    # the relay bot ---------------------------------------------------------
    def react(self, msg: _Raw) -> None:
        text = msg.text.strip()
        if msg.parent_id is not None:
            self._thread_message(msg, text)
        else:
            self._top_message(text)

    def _top_message(self, text: str) -> None:
        if text == "/help":
            self.later(1.0, lambda: self.bot("**Commands**\n`/new` start a session\n`/help` this list"))
            if self.b.help_twice:
                self.later(2.0, lambda: self.bot("Reply inside a thread to talk to it."))
        elif text == "/new":
            self.later(1.0, self._post_new_card)
        elif text.startswith("/new "):
            self.later(1.0, lambda: self.open_session(text.split()[1]))
        elif text.startswith("/models"):
            self.later(1.0, lambda: self.bot("- `default` Default (default)\n- `gpt-5`\n- `gpt-5-mini`"))
        elif text == "/away":
            self.later(1.0, lambda: self.bot("🏝️ Away mode on."))
            if self.b.desk_turn and not self.desk_done:
                self.desk_done = True
                self.later(25.0, self._desk_turn)
        elif text == "/back":
            self.later(1.0, self._back)
        elif text == "/panic":
            self.later(1.0, self._post_panic_card)
        elif text == "/panic now":
            self.later(1.0, self._panic_done)

    def _post_new_card(self) -> None:
        agents = [("Copilot · Default", "vscode_copilot:default"), ("Cursor · Default", "cursor_cli:default")]
        body: list[JsonValue] = [
            {"type": "TextBlock", "text": "New session"},
            {
                "type": "Input.ChoiceSet",
                "id": "ws",
                "value": WORKSPACE,
                "choices": _choices([("scratch · .scratch", WORKSPACE), ("other", "/x/other")]),
            },
            {"type": "Input.ChoiceSet", "id": "agent", "value": agents[0][1], "choices": _choices(agents)},
            {"type": "Input.Text", "id": "prompt", "isRequired": True},
        ]
        self.bot_card("new", "New session", body, [_submit("Start", {"relay": "new", "nonce": "n1"})], None)

    def _new_submitted(self, card_id: str, inputs: dict[str, JsonValue]) -> None:
        for key in ("ws", "agent", "prompt", "relay", "nonce"):
            assert inputs.get(key), key
        assert str(inputs["ws"]).endswith("scratch")
        self.later(1.0, lambda: self.open_session(str(inputs["agent"]).split(":")[0]))

    def _post_model_card(self, root_id: str) -> None:
        if self.b.no_model_card:
            self.bot("Cursor does not support model selection.", root_id)
            return
        models = [("Default", "default")] + (
            [] if self.b.single_model else [("GPT-5", "gpt-5"), ("Mini", "gpt-5-mini")]
        )
        body: list[JsonValue] = [
            {"type": "TextBlock", "text": "Model: default"},
            {"type": "Input.ChoiceSet", "id": "model", "value": "default", "choices": _choices(models)},
        ]
        self.bot_card(
            "model",
            "Model",
            body,
            [_submit("Apply", {"relay": "model", "session": root_id, "nonce": "n2"})],
            root_id,
        )

    def _model_submitted(self, card_id: str, inputs: dict[str, JsonValue]) -> None:
        root_id = str(inputs["session"])
        model = str(inputs["model"])
        self._set_model(root_id, model)

    def _set_model(self, root_id: str, model: str) -> None:
        shown = "gpt-4" if self.b.wrong_model_reply else model
        self.later(1.0, lambda: self.bot(f"Model → `{shown}` from the next turn.", root_id))
        self.roots[root_id]["model"] = model
        self.refresh_root(root_id)

    def _post_panic_card(self) -> None:
        body: list[JsonValue] = [{"type": "TextBlock", "text": "Cancel everything?"}]
        actions = [
            _submit("Cancel everything", {"relay": "panic", "op": "confirm", "nonce": "n3"}),
            _submit("Cancel", {"relay": "panic", "op": "cancel", "nonce": "n3"}),
        ]
        self.bot_card("panic", "Cancel everything?", body, actions, None)

    def _panic_submitted(self, card_id: str, inputs: dict[str, JsonValue]) -> None:
        assert inputs["op"] == "confirm"
        self.later(1.0, self._panic_done)

    def _panic_done(self) -> None:
        self.paused = True
        self.bot("Stopped 0 turns and paused the relay. `/away` resumes.")
        if not self.b.panic_keeps_running:
            for info in self.roots.values():
                if "⏳" in info["status"]:
                    info["status"] = "💤 idle"
        for root_id in self.roots:
            self.refresh_root(root_id)

    def _desk_turn(self) -> None:
        self.open_session("cursor", parked=True)

    def _back(self) -> None:
        self.bot("Away mode off.")
        if not self.b.back_keeps_parked:
            for root_id, info in self.roots.items():
                if "🅿️" in info["status"]:
                    info["status"] = "💤 idle"
                    self.refresh_root(root_id)

    def _thread_message(self, msg: _Raw, text: str) -> None:
        root_id = msg.parent_id or msg.id
        name = self.uploads.get(msg.id)
        if text == "/model":
            self.later(1.0, lambda: self._post_model_card(root_id))
        elif text.startswith("/model "):
            self._set_model(root_id, text.split()[1])
        elif text == "/stop":
            self._stop(root_id)
        elif "integers from 1 to 800" in text:
            self._long_turn(root_id)
        elif name is not None and not text:
            self.held.append(name)
        elif name is not None:
            names = [*self.held, name]
            self.held = []
            said = "I see no files." if self.b.ignore_files else ", ".join(names)
            self.later(self.b.final_delay, lambda: self.finish(root_id, said))
        elif "markdown tables" in text:
            files = [] if self.b.no_html_file else [ANSWER_URL]
            self.later(
                self.b.final_delay, lambda: self.finish(root_id, "Preview of the tables.", files=files)
            )
        elif "relay-e2e-$((6*7))" in text:
            if self.b.approval_card:
                self.later(2.0, lambda: self._post_approval(root_id))
            self.later(self.b.final_delay, lambda: self.finish(root_id, "relay-e2e-42"))
        else:
            self.later(self.b.final_delay, lambda: self.finish(root_id, self.b.final_text))

    def _long_turn(self, root_id: str) -> None:
        self.roots[root_id]["status"] = "⏳ running"
        self.refresh_root(root_id)
        self.running_long.add(root_id)
        self.later(120.0, lambda: self._long_done(root_id))

    def _long_done(self, root_id: str) -> None:
        if root_id in self.running_long:
            self.running_long.discard(root_id)
            self.finish(root_id, "1\n2\n3")

    def _stop(self, root_id: str) -> None:
        self.bot("⏹️ Stopped.", root_id)
        if self.b.stop_ignored:
            return
        self.running_long.discard(root_id)
        self.roots[root_id]["status"] = "💤 idle"
        self.refresh_root(root_id)

    def _post_approval(self, root_id: str) -> None:
        body: list[JsonValue] = [{"type": "TextBlock", "text": "Run a shell command?"}]
        actions = [
            _submit("Allow", {"relay": "approval", "decision": "allow"}),
            _submit("Deny", {"relay": "approval", "decision": "deny"}),
        ]
        self.bot_card("approval", "Run a shell command?", body, actions, root_id)
