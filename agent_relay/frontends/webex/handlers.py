"""Act on parsed commands and card submissions by calling RelayService, replying through the renderer."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pydantic import JsonValue

from agent_relay import paths
from agent_relay.core import away
from agent_relay.core.inbox import Inbox, receipt_line
from agent_relay.core.service import APP_LABELS, AppInfo, RelayError, RelayService, resolve_app
from agent_relay.frontends import card_state, commands, registry
from agent_relay.frontends.apps import app_emoji
from agent_relay.frontends.card_state import InvalidInput
from agent_relay.frontends.commands import Command, HelpHint, Invalid, Prompt, Unknown
from agent_relay.frontends.surface import Surface
from agent_relay.frontends.webex import cards
from agent_relay.log import get_logger
from agent_relay.models.attachments import Received
from agent_relay.models.capabilities import Capability
from agent_relay.models.events import Harness
from agent_relay.models.sessions import SessionRecord
from agent_relay.settings import load_workspaces, resolve_workspace

log = get_logger(__name__)

DEFAULT_MODEL = "__default__"
NO_MODEL = ("", DEFAULT_MODEL, "default", "-")
LAST_WORKSPACE_KEY = "new.last_workspace"
DRAFT_PREFIX = "new.draft:"  # card 1 of the two-card layout leaves its answers here for card 2


@dataclass(frozen=True)
class Context:
    """Where a message came from: a session thread, or the top level."""

    session: SessionRecord | None


async def health_summary(service: RelayService) -> str:
    apps = await service.apps()
    if not apps:
        return "No adapters are enabled."
    lines = ["**Health**"]
    lines += [f"- {'✅' if a.healthy else '❌'} {a.label}: {a.detail}" for a in apps]
    pending = await service.registry.open_pending()
    lines.append(f"- pending requests: {len(pending)}")
    return "\n".join(lines)


class Handlers:
    def __init__(
        self,
        service: RelayService,
        renderer: Surface,
        doctor: Callable[[], Awaitable[str]] | None = None,
        inbox: Inbox | None = None,
    ) -> None:
        self.service = service
        self.renderer = renderer
        self.doctor = doctor or (lambda: health_summary(service))
        self.inbox = inbox

    async def reply(self, ctx: Context, markdown: str, file: tuple[str, bytes, str] | None = None) -> None:
        if ctx.session is not None:
            await self.renderer.post_in_thread(ctx.session, markdown, file=file)
        else:
            await self.renderer.post_top(markdown)

    async def card(self, ctx: Context, fallback: str, card: dict[str, JsonValue]) -> None:
        if ctx.session is not None:
            await self.renderer.post_in_thread(ctx.session, fallback, card=card)
        else:
            await self.renderer.post_top(fallback, card=card)

    async def text(self, ctx: Context, text: str, received: Received | None = None) -> None:
        parsed = commands.parse(text, in_thread=ctx.session is not None)
        try:
            await self._dispatch(ctx, parsed, received)
        except RelayError as exc:
            await self.reply(ctx, f"⚠️ {exc}")
        except Exception as exc:
            log.exception("command failed")
            await self.reply(ctx, f"⚠️ That failed ({type(exc).__name__}). `/doctor` may help.")

    async def _dispatch(
        self, ctx: Context, parsed: commands.Parsed, received: Received | None = None
    ) -> None:
        match parsed:
            case HelpHint():
                await self.reply(ctx, commands.HELP_HINT)
            case Unknown(name=name):
                await self.reply(ctx, f"Unknown command `/{name}`. `/help` lists commands.")
            case Invalid(reason=reason):
                await self.reply(ctx, reason)
            case Prompt(text=text):
                assert ctx.session is not None
                files = received.attachments if received else ()
                receipt = await self.service.prompt(ctx.session.session_key, text, files)
                if receipt != "sent":
                    await self.reply(ctx, f"📨 {receipt}")
                if received:
                    await self._report_files(ctx, ctx.session.harness, received, receipt.startswith("sent"))
            case Command(name=name):
                spec = registry.lookup(name)
                if spec is None:
                    await self.reply(ctx, f"Unknown command `/{name}`. `/help` lists commands.")
                else:
                    await spec.handler(self, ctx, parsed)

    def require(self, ctx: Context, capability: Capability, what: str) -> SessionRecord:
        """The thread's session, if its app can do `what`; otherwise a user-facing error."""
        assert ctx.session is not None
        adapter = self.service.router.adapter(ctx.session.harness)
        if adapter is None or not adapter.capabilities.has(capability):
            label = APP_LABELS.get(ctx.session.harness, ctx.session.harness.value)
            raise RelayError(f"{label} does not support {what}.")
        return ctx.session

    # /new: one card (or two), or the typed one-liner

    async def _nonce(self) -> str:
        return await card_state.issue_nonce(self.service.registry)

    async def _validate(self, carry: dict[str, JsonValue]) -> None:
        """Whatever a card (or a typed `/new`) carries must be something the relay offers."""
        svc = self.service
        values = [carry.get(f) for f in ("app", "ws", "model", "prompt")]
        if any(v is not None and not isinstance(v, str) for v in values):
            raise InvalidInput("That card carried something unexpected.")
        app, ws, model, prompt = (str(v or "") for v in values)
        harness = resolve_app(app) if app else None
        adapter = svc.router.adapter(harness) if harness else None
        label = APP_LABELS.get(harness, harness.value) if harness else ""
        if harness and adapter is None:
            raise InvalidInput(f"{label} is not enabled.")
        if adapter and not adapter.capabilities.has(Capability.COLD_START):
            raise InvalidInput(f"{label} cannot start sessions from the phone.")
        if ws and resolve_workspace(ws, load_workspaces()) is None:
            raise InvalidInput(f"Unknown workspace `{ws}`. Try `/ws`.")
        if harness and adapter and model not in NO_MODEL:
            if not adapter.capabilities.has(Capability.MODEL_SELECT):
                raise InvalidInput(f"{label} does not offer model selection.")
            if not any(model in (m.id, m.label) for m in await svc.models(harness.value)):
                raise InvalidInput(f"{label} has no model `{model}`. Try `/models {harness.value}`.")
        if len(prompt) > cards.MAX_PROMPT_CHARS:
            raise InvalidInput(f"That prompt is too long ({cards.MAX_PROMPT_CHARS} characters at most).")

    async def wizard(self, ctx: Context, carry: dict[str, JsonValue]) -> None:
        """Start the session when the typed `/new` has app, workspace and prompt; otherwise one card."""
        await self._validate(carry)
        app, ws, model, prompt = (carry.get(f) for f in ("app", "ws", "model", "prompt"))
        if (
            isinstance(app, str)
            and isinstance(ws, str)
            and isinstance(prompt, str)
            and app
            and ws
            and prompt.strip()
        ):
            await self._start(app, ws, model if isinstance(model, str) else "", prompt)
            return
        await self._new_card(carry)

    async def _report_files(self, ctx: Context, harness: Harness, received: Received, sent: bool) -> None:
        """Say which files the app got as attachments and which as a path (queued ones are always paths)."""
        adapter = self.service.router.adapter(harness)
        native = sent and adapter is not None and adapter.capabilities.has(Capability.ATTACH_FILES)
        await self.reply(ctx, receipt_line(received, native))

    async def _start_session(
        self, harness: Harness, ws: str, text: str, model: str | None, workspace: str
    ) -> SessionRecord:
        """Start the session with any files that came with `/new`, and tell the thread what happened."""
        svc = self.service
        received = None
        if self.inbox is not None:
            received = await self.inbox.take_for_new(paths.expand_user(workspace))
        try:
            session = await svc.start(
                harness.value, ws, text, model, received.attachments if received else ()
            )
        except BaseException:
            if received is not None and self.inbox is not None:
                await self.inbox.discard(received)
            raise
        if received is not None and self.inbox is not None:
            await self.inbox.claim(session.session_key, received)
        await self.renderer.thread_for(session)
        if received is not None:
            await self._report_files(Context(session), harness, received, True)
        return session

    async def _start(self, app: str, ws: str, model: str, prompt: str, *, away_on: bool = False) -> None:
        svc = self.service
        text = prompt.strip()
        if not text:
            raise InvalidInput("Say what the session should do.")
        target = resolve_workspace(ws, load_workspaces())
        if target is None:
            raise InvalidInput(f"Unknown workspace `{ws}`. Try `/ws`.")
        harness = resolve_app(app)
        if away_on and not await away.is_away(svc.registry, target.path):
            await svc.set_away_for(True, target.path)
        session = await self._start_session(
            harness, ws, text, None if model in NO_MODEL else model, target.path
        )
        await svc.registry.kv_set(LAST_WORKSPACE_KEY, target.name)
        await self.renderer.thread_for(session)

    async def _app_option(self, info: AppInfo, preferred: JsonValue) -> cards.AppOption:
        """One app for the card: its listed models (utility ones hidden) and which to preselect."""
        svc = self.service
        base = cards.AppOption(info.harness.value, info.label, app_emoji(info.harness))
        if Capability.MODEL_SELECT not in info.capabilities:
            return base
        try:
            listed = {
                m.id: m.label
                for m in await svc.models(info.harness.value)
                if not cards.is_hidden_model(m.id, m.label)
            }
            sticky = await svc.sticky_model(info.harness)
        except RelayError:
            return base
        if not listed:
            return base
        by_label = {label: mid for mid, label in listed.items()}
        selected = DEFAULT_MODEL
        if isinstance(preferred, str) and preferred in NO_MODEL[1:]:
            selected = DEFAULT_MODEL
        elif isinstance(preferred, str) and preferred in {**listed, **by_label}:
            selected = preferred if preferred in listed else by_label[preferred]
        elif sticky is not None and sticky in listed:
            selected = sticky
        models = ((DEFAULT_MODEL, "Default"), *listed.items())
        return cards.AppOption(base.harness, base.label, base.emoji, models, selected)

    async def _app_options(self, carry: dict[str, JsonValue]) -> list[cards.AppOption]:
        wanted = carry.get("app")
        only = resolve_app(wanted) if isinstance(wanted, str) and wanted else None
        options: list[cards.AppOption] = []
        for info in await self.service.apps():
            if Capability.COLD_START in info.capabilities and only in (None, info.harness):
                options.append(await self._app_option(info, carry.get("model")))
        return options

    async def _default_workspace(self, carry: dict[str, JsonValue], names: list[str]) -> str:
        wanted = carry.get("ws")
        if isinstance(wanted, str) and wanted in names:
            return wanted
        last = await self.service.registry.kv_get(LAST_WORKSPACE_KEY)
        return last if last in names else names[0]

    async def _new_card(self, carry: dict[str, JsonValue]) -> None:
        top = Context(None)
        spaces = self.service.workspaces()
        if not spaces:
            await self.reply(top, "No workspaces. Add one with `relay ws add <name> <path>`.")
            return
        options = await self._app_options(carry)
        if not options:
            await self.reply(top, "No app that can start sessions is available. `/doctor` shows why.")
            return
        choices = [(w.name, f"{w.name} · {w.file or w.path}") for w in spaces]
        workspace = await self._default_workspace(carry, [w.name for w in spaces])
        prompt = carry.get("prompt")
        draft = prompt if isinstance(prompt, str) else None
        nonce = await self._nonce()
        if self.service.config.webex.new_card_layout == "two":
            app = options[0].harness if len(options) == 1 else None
            card = cards.new_details(choices, workspace, draft, options, app, nonce=nonce)
        else:
            card = cards.new_session(choices, workspace, draft, options, nonce=nonce)
        await self.card(top, "New session", card)

    async def _start_from_card(self, carry: dict[str, JsonValue], away_on: object) -> None:
        """Re-check everything a card carried, then start."""
        await self._validate(carry)
        app, ws = carry.get("app"), carry.get("ws")
        if not isinstance(app, str) or not app:
            raise InvalidInput("Pick an app.")
        if not isinstance(ws, str) or not ws:
            raise InvalidInput("Pick a workspace.")
        model, prompt = carry.get("model"), carry.get("prompt")
        await self._start(
            app,
            ws,
            model if isinstance(model, str) else "",
            prompt if isinstance(prompt, str) else "",
            away_on=away_on in (True, "true"),
        )

    async def _new_start(self, inputs: dict[str, JsonValue]) -> None:
        app = inputs.get("app")
        if not isinstance(app, str) or not app:
            raise InvalidInput("Pick an app.")
        carry: dict[str, JsonValue] = {
            "app": app,
            "ws": inputs.get("ws"),
            "prompt": inputs.get("prompt"),
            "model": inputs.get(f"model_{resolve_app(app).value}"),
        }
        await self._start_from_card(carry, inputs.get("away"))

    async def _new_details(self, inputs: dict[str, JsonValue]) -> None:
        carry: dict[str, JsonValue] = {k: inputs.get(k) for k in ("app", "ws", "prompt")}
        await self._validate(carry)
        prompt = carry.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise InvalidInput("Say what the session should do.")
        options = await self._app_options(carry)
        if not options:
            raise InvalidInput("Pick an app.")
        away_on = inputs.get("away")
        if not options[0].models:
            await self._start_from_card(carry, away_on)
            return
        nonce = await self._nonce()
        await self.service.registry.kv_set(
            f"{DRAFT_PREFIX}{nonce}", json.dumps({**carry, "away": away_on in (True, "true")})
        )
        await self.card(Context(None), "Pick a model", cards.new_model(options[0], nonce=nonce))

    async def _new_go(self, inputs: dict[str, JsonValue]) -> None:
        nonce = inputs.get("nonce")
        raw = await self.service.registry.kv_get(f"{DRAFT_PREFIX}{nonce}") if isinstance(nonce, str) else None
        draft = json.loads(raw) if raw else None
        if not isinstance(draft, dict):
            raise InvalidInput("That draft expired. Send `/new` again.")
        carry: dict[str, JsonValue] = {k: draft.get(k) for k in ("app", "ws", "prompt")}
        carry["model"] = inputs.get("model")
        await self._start_from_card(carry, draft.get("away"))
        await self.service.registry.kv_delete(f"{DRAFT_PREFIX}{nonce}")

    async def new_step(self, inputs: dict[str, JsonValue]) -> None:
        step = inputs.get("step")
        if step == "start":
            await self._new_start(inputs)
            return
        if step == "details":
            await self._new_details(inputs)
            return
        if step == "go":
            await self._new_go(inputs)
            return
        value = inputs.get("value")
        carry = cards.carry_fields(inputs)
        if isinstance(step, str) and step in ("app", "ws", "model", "prompt"):
            carry[step] = value if isinstance(value, str) else ""
            if step == "model" and not carry[step]:
                carry[step] = DEFAULT_MODEL
        await self.wizard(Context(None), carry)
