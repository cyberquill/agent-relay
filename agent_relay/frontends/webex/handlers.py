"""Act on parsed commands and card submissions by calling RelayService, replying through the renderer."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pydantic import JsonValue

from agent_relay.core import away
from agent_relay.core.service import APP_LABELS, RelayError, RelayService, resolve_app
from agent_relay.frontends import commands
from agent_relay.frontends.commands import Command, CommandName, HelpHint, Invalid, Prompt, Unknown
from agent_relay.frontends.surface import Surface
from agent_relay.frontends.webex import cards
from agent_relay.log import get_logger
from agent_relay.models.capabilities import Capability
from agent_relay.models.sessions import SessionRecord

log = get_logger(__name__)

DEFAULT_MODEL = "__default__"


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
    ) -> None:
        self._service = service
        self._renderer = renderer
        self._doctor = doctor or (lambda: health_summary(service))

    async def reply(self, ctx: Context, markdown: str, file: tuple[str, bytes, str] | None = None) -> None:
        if ctx.session is not None:
            await self._renderer.post_in_thread(ctx.session, markdown, file=file)
        else:
            await self._renderer.post_top(markdown)

    async def card(self, ctx: Context, fallback: str, card: dict[str, JsonValue]) -> None:
        if ctx.session is not None:
            await self._renderer.post_in_thread(ctx.session, fallback, card=card)
        else:
            await self._renderer.post_top(fallback, card=card)

    async def text(self, ctx: Context, text: str) -> None:
        parsed = commands.parse(text, in_thread=ctx.session is not None)
        try:
            await self._dispatch(ctx, parsed)
        except RelayError as exc:
            await self.reply(ctx, f"⚠️ {exc}")
        except Exception as exc:
            log.exception("command failed")
            await self.reply(ctx, f"⚠️ That failed ({type(exc).__name__}). `/doctor` may help.")

    async def _dispatch(self, ctx: Context, parsed: commands.Parsed) -> None:
        match parsed:
            case HelpHint():
                await self.reply(ctx, commands.HELP_HINT)
            case Unknown(name=name):
                await self.reply(ctx, f"Unknown command `/{name}`. `/help` lists commands.")
            case Invalid(reason=reason):
                await self.reply(ctx, reason)
            case Prompt(text=text):
                assert ctx.session is not None
                receipt = await self._service.prompt(ctx.session.session_key, text)
                if receipt != "sent":
                    await self.reply(ctx, f"📨 {receipt}")
            case Command():
                await self._command(ctx, parsed)

    def _require(self, ctx: Context, capability: Capability, what: str) -> SessionRecord:
        assert ctx.session is not None
        adapter = self._service.router.adapter(ctx.session.harness)
        if adapter is None or not adapter.capabilities.has(capability):
            label = APP_LABELS.get(ctx.session.harness, ctx.session.harness.value)
            raise RelayError(f"{label} does not support {what}.")
        return ctx.session

    async def _command(self, ctx: Context, cmd: Command) -> None:
        svc = self._service
        match cmd.name:
            case CommandName.HELP:
                await self.reply(ctx, commands.HELP_TEXT)
            case CommandName.NEW:
                app, ws, model = (list(cmd.args) + [None, None, None])[:3]
                await self.wizard(ctx, {"app": app, "ws": ws, "model": model, "prompt": cmd.prompt})
            case CommandName.SESSIONS:
                sessions = await svc.sessions()
                lines = [f"- {cards.root_markdown(s)}" for s in sessions] or ["No active sessions."]
                await self.reply(ctx, "\n".join(lines))
            case CommandName.APPS:
                apps = await svc.apps()
                lines = [
                    f"- {'✅' if a.healthy else '❌'} **{a.label}** (`{a.harness.value}`): "
                    f"{', '.join(c.value for c in a.capabilities) or 'observe only'}"
                    for a in apps
                ]
                await self.reply(ctx, "\n".join(lines) or "No adapters are enabled.")
            case CommandName.WS:
                spaces = svc.workspaces()
                lines = [f"- **{w.name}** `{w.path}`" for w in spaces] or [
                    "No workspaces. Add them with `relay ws add <name> <path>`."
                ]
                await self.reply(ctx, "\n".join(lines))
            case CommandName.MODELS:
                models = await svc.models(cmd.args[0])
                lines = [f"- `{m.id}` {m.label}{' (default)' if m.default else ''}" for m in models]
                await self.reply(ctx, "\n".join(lines) or "No models reported.")
            case CommandName.MODEL:
                session = self._require(ctx, Capability.MODEL_SELECT, "model selection")
                if cmd.args:
                    await svc.set_model(session.session_key, cmd.args[0])
                    await self.reply(ctx, f"Model set to `{cmd.args[0]}`.")
                    await self._renderer.refresh_root(await svc.session(session.session_key))
                else:
                    current = (await svc.session(session.session_key)).model or "default"
                    await self.reply(ctx, f"Model: `{current}`")
            case CommandName.STOP:
                session = self._require(ctx, Capability.CANCEL, "cancel")
                await svc.stop(session.session_key)
                await self.reply(ctx, "⏹️ Stopped.")
            case CommandName.STATUS:
                assert ctx.session is not None
                await self.reply(ctx, await svc.status(ctx.session.session_key))
            case CommandName.DIFF:
                assert ctx.session is not None
                diff = await svc.diff(ctx.session.session_key)
                if not diff.strip():
                    await self.reply(ctx, "No changes.")
                else:
                    await self.reply(ctx, "📎 changes.diff", file=("changes.diff", diff, "text/x-diff"))
            case CommandName.AWAY:
                key = ctx.session.session_key if ctx.session else None
                if not cmd.args:
                    workspace = ctx.session.workspace if ctx.session else None
                    state = await away.is_away(svc.registry, workspace)
                    await self.reply(ctx, f"Away mode is {'on' if state else 'off'}.")
                else:
                    await self.reply(ctx, await svc.set_away(cmd.args[0] == "on", key))
            case CommandName.BACK:
                key = None
                if ctx.session is not None:
                    key = self._require(ctx, Capability.PARK, "parking").session_key
                await self.reply(ctx, await svc.back(key))
            case CommandName.DOCTOR:
                await self.reply(ctx, await self._doctor())
            case CommandName.PANIC:
                await self.reply(ctx, await svc.panic())

    # /new wizard: app → workspace → model → prompt

    async def wizard(self, ctx: Context, carry: dict[str, JsonValue]) -> None:
        svc = self._service
        top = Context(None)
        app = carry.get("app")
        if not isinstance(app, str) or not app:
            apps = [a for a in await svc.apps() if Capability.COLD_START in a.capabilities]
            if not apps:
                await self.reply(top, "No app that can start sessions is available. `/doctor` shows why.")
                return
            choices = [(a.harness.value, a.label) for a in apps]
            await self.card(top, "Pick an app", cards.choose("app", "Which app?", choices, _strip(carry)))
            return
        harness = resolve_app(app)
        ws = carry.get("ws")
        if not isinstance(ws, str) or not ws:
            spaces = svc.workspaces()
            if not spaces:
                await self.reply(top, "No workspaces. Add one with `relay ws add <name> <path>`.")
                return
            choices = [(w.name, f"{w.name} · {w.path}") for w in spaces]
            await self.card(
                top, "Pick a workspace", cards.choose("ws", "Which workspace?", choices, _strip(carry))
            )
            return
        model = carry.get("model")
        adapter = svc.router.adapter(harness)
        if (
            (model is None or model == "")
            and adapter is not None
            and adapter.capabilities.has(Capability.MODEL_SELECT)
        ):
            sticky = await svc.sticky_model(harness)
            models = await svc.models(harness.value)
            choices = [(DEFAULT_MODEL, f"Default{f' ({sticky})' if sticky else ''}")]
            choices += [(m.id, m.label) for m in models]
            await self.card(
                top, "Pick a model", cards.choose("model", "Which model?", choices, _strip(carry))
            )
            return
        prompt = carry.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            await self.card(top, "What should it do?", cards.prompt(_strip(carry)))
            return
        chosen = (
            model if isinstance(model, str) and model not in ("", DEFAULT_MODEL, "default", "-") else None
        )
        session = await svc.start(harness.value, ws, prompt.strip(), chosen)
        await self._renderer.thread_for(session)

    async def new_step(self, inputs: dict[str, JsonValue]) -> None:
        step = inputs.get("step")
        value = inputs.get("value")
        carry = _strip(inputs)
        if isinstance(step, str) and step in ("app", "ws", "model", "prompt"):
            carry[step] = value if isinstance(value, str) else ""
            if step == "model" and not carry[step]:
                carry[step] = DEFAULT_MODEL
        try:
            await self.wizard(Context(None), carry)
        except RelayError as exc:
            await self.reply(Context(None), f"⚠️ {exc}")


def _strip(values: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Keep only the wizard fields that carry between steps."""
    return {k: v for k, v in values.items() if k in ("app", "ws", "model", "prompt") and v not in (None, "")}
