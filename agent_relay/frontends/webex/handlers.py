"""Act on parsed commands and card submissions by calling RelayService, replying through the renderer."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pydantic import JsonValue

from agent_relay import paths
from agent_relay.core.inbox import Inbox, receipt_line
from agent_relay.core.service import APP_LABELS, STICKY_MODEL_KEY, RelayError, RelayService, resolve_app
from agent_relay.core.service.discovery import (
    DEFAULT_CHOICE,
    LAST_APP_KEY,
    AgentOffer,
    agent_value,
    visible_models,
)
from agent_relay.frontends import card_state, commands, registry
from agent_relay.frontends.apps import app_emoji
from agent_relay.frontends.card_state import InvalidInput
from agent_relay.frontends.commands import Command, HelpHint, Invalid, Prompt, Unknown
from agent_relay.frontends.surface import Surface
from agent_relay.frontends.webex import cards
from agent_relay.gateway.signin import SignIn
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
DRAFT_PREFIX = "new.draft:"  # drafts the old two-card layout left; core.housekeeping still sweeps them


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
    """Commands and card taps for the one Webex space (or the PWA); replies go through `renderer`."""

    def __init__(
        self,
        service: RelayService,
        renderer: Surface,
        doctor: Callable[[], Awaitable[str]] | None = None,
        inbox: Inbox | None = None,
        mcp: SignIn | None = None,
    ) -> None:
        self.service = service
        self.renderer = renderer
        self.doctor = doctor or (lambda: health_summary(service))
        self.inbox = inbox
        self.mcp = mcp

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
        if self.mcp is not None and (finished := await self.mcp.complete_from_url(text)) is not None:
            await self.reply(ctx, finished)
            return
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

    # /new: one flat card, or the typed one-liner

    async def _nonce(self) -> str:
        return await card_state.issue_nonce(self.service.registry)

    async def _validate(self, carry: dict[str, JsonValue]) -> str | None:
        """Whatever a typed `/new` carries must be something the relay offers. Returns the model id."""
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
        if len(prompt) > cards.MAX_PROMPT_CHARS:
            raise InvalidInput(f"That prompt is too long ({cards.MAX_PROMPT_CHARS} characters at most).")
        if not (harness and adapter) or model in NO_MODEL:
            return None
        if not adapter.capabilities.has(Capability.MODEL_SELECT):
            raise InvalidInput(f"{label} does not offer model selection.")
        return await self.valid_model(harness, model)

    async def valid_model(self, harness: Harness, model: str) -> str:
        """The id of the app's model that `model` (an id or a label) names, or an error with a hint."""
        listed = visible_models(await self.service.models(harness.value))
        found = next((m.id for m in listed if model in (m.id, m.label)), None)
        if found is None:
            label = APP_LABELS.get(harness, harness.value)
            hint = f"Try `/models {harness.value}`."
            if harness is Harness.CURSOR_CDP and not listed:
                hint = f"Its models are not known yet: `/models {harness.value}` lists and saves them."
            raise InvalidInput(f"{label} has no model `{model}`. {hint}")
        return found

    async def start_or_card(self, ctx: Context, carry: dict[str, JsonValue]) -> None:
        """Start the session when the typed `/new` has app, workspace and prompt; otherwise one card."""
        model = await self._validate(carry)
        app, ws, prompt = (carry.get(f) for f in ("app", "ws", "prompt"))
        if (
            isinstance(app, str)
            and isinstance(ws, str)
            and isinstance(prompt, str)
            and app
            and ws
            and prompt.strip()
        ):
            await self._start(app, ws, model, prompt)
            return
        await self._new_card(carry)

    async def _report_files(self, ctx: Context, harness: Harness, received: Received, sent: bool) -> None:
        """Say, file by file, what the app got as an attachment and what as a path.

        Only a direct send to an app that takes files is an attachment; a woken, queued or parked
        delivery carries paths.
        """
        adapter = self.service.router.adapter(harness)
        native = sent and adapter is not None and adapter.capabilities.has(Capability.ATTACH_FILES)
        attached = (
            frozenset(a.local_path for a in received.attachments if adapter and adapter.takes_file(a.mime))
            if native
            else frozenset()
        )
        await self.reply(ctx, receipt_line(received, attached))

    async def _start_session(
        self, harness: Harness, ws: str, text: str, model: str | None, workspace: str
    ) -> SessionRecord:
        """Start the session with any files that came with `/new`.

        Raises only while there is no session; once it exists, a failure after it is logged, so a tap
        is never reported as failed (and retried) when a session is already running.
        """
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
                await self.inbox.restore_new(received)
            raise
        try:
            if received is not None and self.inbox is not None:
                await self.inbox.claim(session.session_key, received)
            await self.renderer.thread_for(session)
            if received is not None:
                await self._report_files(Context(session), harness, received, True)
        except Exception:
            log.exception(f"session {session.session_key} started, but its thread was not set up")
        return session

    async def _start(
        self, app: str, ws: str, model: str | None, prompt: str, *, reset_sticky: bool = False
    ) -> SessionRecord:
        registry = self.service.registry
        text = prompt.strip()
        if not text:
            raise InvalidInput("Say what the session should do.")
        target = resolve_workspace(ws, load_workspaces())
        if target is None:
            raise InvalidInput(f"Unknown workspace `{ws}`. Try `/ws`.")
        harness = resolve_app(app)
        if reset_sticky:
            # The app's own default was picked: not the model last used with it.
            await registry.kv_delete(STICKY_MODEL_KEY.format(harness=harness.value))
        session = await self._start_session(harness, ws, text, model, target.path)
        try:
            await registry.kv_set(LAST_WORKSPACE_KEY, target.name)
            await registry.kv_set(LAST_APP_KEY, harness.value)
        except Exception:
            log.exception("could not remember the last workspace and app")
        return session

    async def _default_workspace(self, carry: dict[str, JsonValue], names: list[str]) -> str:
        wanted = carry.get("ws")
        if isinstance(wanted, str) and wanted in names:
            return wanted
        last = await self.service.registry.kv_get(LAST_WORKSPACE_KEY)
        return last if last in names else names[0]

    def _prefilled_agent(self, offer: AgentOffer, carry: dict[str, JsonValue]) -> str | None:
        """The agent a partial `/new app ws model` already names, else the offer's own preselection."""
        app, model = carry.get("app"), carry.get("model")
        if isinstance(app, str) and app:
            harness = resolve_app(app).value
            typed = None if not isinstance(model, str) or model in NO_MODEL else model
            for value in (agent_value(harness, typed), agent_value(harness, None)):
                if offer.find(value) is not None:
                    return value
        return offer.preselected

    async def _held_files(self) -> int:
        """How many files sent at the top level wait for the next `/new`."""
        held = await self.inbox.held_top_level() if self.inbox is not None else None
        return len(held.attachments) if held is not None else 0

    async def _new_card(self, carry: dict[str, JsonValue]) -> None:
        top = Context(None)
        spaces = self.service.workspaces()
        if not spaces:
            await self.reply(top, "No workspaces. Add one with `relay ws add <name> <path>`.")
            return
        offer = await self.service.agent_offer()
        if not offer.entries:
            await self.reply(top, "No app that can start sessions is available. `/doctor` shows why.")
            return
        workspace = await self._default_workspace(carry, [w.name for w in spaces])
        prompt = carry.get("prompt")
        nonce = await self._nonce()
        await card_state.store_offer(self.service.registry, nonce, tuple(e.value for e in offer.entries))
        card = cards.new_session(
            [(w.name, f"{w.name} · {w.file or w.path}") for w in spaces],
            workspace,
            [(e.value, f"{app_emoji(Harness(e.harness))} {e.label}") for e in offer.entries],
            self._prefilled_agent(offer, carry),
            prompt if isinstance(prompt, str) else None,
            nonce=nonce,
            unavailable=offer.unavailable,
            files=await self._held_files(),
        )
        await self.card(top, "New session", card)

    async def new_submit(self, inputs: dict[str, JsonValue]) -> None:
        """A `/new` card's Start button. Raises (so the card stays tappable) until a session exists."""
        if "agent" not in inputs:
            # The `/ws` card's "Start a session here" button: open the card with that workspace chosen.
            if inputs.get("step") != "ws":
                raise InvalidInput("Pick an agent.")
            ws = inputs.get("value")
            await self._new_card({"ws": ws if isinstance(ws, str) else None})
            return
        registry = self.service.registry
        agent, ws, prompt, nonce = (inputs.get(k) for k in ("agent", "ws", "prompt", "nonce"))
        if not isinstance(agent, str) or not isinstance(ws, str) or not isinstance(prompt, (str, type(None))):
            raise InvalidInput("That card carried something unexpected.")
        if agent not in await card_state.load_offer(registry, nonce if isinstance(nonce, str) else ""):
            raise InvalidInput("That agent was not one this card offered. Send `/new` again.")
        if not ws:
            raise InvalidInput("Pick a workspace.")
        if len(prompt or "") > cards.MAX_PROMPT_CHARS:
            raise InvalidInput(f"That prompt is too long ({cards.MAX_PROMPT_CHARS} characters at most).")
        app, _, model = agent.partition(":")
        resolve_app(app)
        chosen = None if model == DEFAULT_CHOICE else model
        try:
            await self._start(app, ws, chosen, prompt or "", reset_sticky=chosen is None)
        except RelayError:
            raise
        except Exception as exc:
            log.exception("starting a session from the card failed")
            raise RelayError(
                f"The session could not be started ({type(exc).__name__}). `/doctor` may help."
            ) from exc
        if isinstance(nonce, str):
            await card_state.drop_offer(registry, nonce)
