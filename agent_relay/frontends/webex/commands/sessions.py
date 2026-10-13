"""/new, /sessions, /status, /stop, /diff, /model and /models."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from pydantic import JsonValue

from agent_relay.adapters.base import ModelInfo
from agent_relay.core.service import RelayError, RelayService, resolve_app
from agent_relay.frontends import card_state
from agent_relay.frontends.actions import Outcome, register_action
from agent_relay.frontends.card_state import InvalidInput
from agent_relay.frontends.commands import Command, Invalid
from agent_relay.frontends.registry import CommandSpec, register
from agent_relay.frontends.webex import cards
from agent_relay.frontends.webex.handlers import DEFAULT_MODEL, Context, Handlers
from agent_relay.models.capabilities import Capability

if TYPE_CHECKING:
    from agent_relay.models.sessions import SessionRecord

_OPENING = re.compile(r"""(?<!\S)["']""")


def _quoted_prompt(rest: str) -> tuple[str, str] | None:
    """The first quoted span as (body, text without it). A quote opens a token and closes before
    whitespace or the end, so the apostrophes in `don't` are ordinary characters."""
    for opening in _OPENING.finditer(rest):
        quote = opening.group()
        closing = re.compile(f"{re.escape(quote)}(?=\\s|\\Z)").search(rest, opening.end())
        if closing:
            return rest[opening.end() : closing.start()], rest[: opening.start()] + " " + rest[
                closing.end() :
            ]
    return None


def _parse_new(rest: str) -> Command | Invalid:
    prompt: str | None = None
    quoted = _quoted_prompt(rest)
    if quoted:
        prompt = quoted[0].strip() or None
        rest = quoted[1]
    tokens = rest.split()
    positional, extra = tokens[:3], tokens[3:]
    if extra:
        if prompt is not None:
            return Invalid("new", "too many arguments: put the prompt in quotes", tuple(tokens))
        prompt = " ".join(extra)
    return Command("new", tuple(positional), prompt)


async def _is_model(h: Handlers, app: str, word: str) -> bool:
    """`/new vscode scratch Run tests` has no model: the third word is one only if the app lists it."""
    if word in (DEFAULT_MODEL, "default", "-"):
        return True
    adapter = h.service.router.adapter(resolve_app(app))
    if adapter is None or not adapter.capabilities.has(Capability.MODEL_SELECT):
        return False
    return any(word in (m.id, m.label) for m in await h.service.models(resolve_app(app).value))


async def _new(h: Handlers, ctx: Context, cmd: Command) -> None:
    app, ws, model = (list(cmd.args) + [None, None, None])[:3]
    prompt = cmd.prompt
    if app and model and not await _is_model(h, app, model):
        prompt, model = " ".join(p for p in (model, prompt) if p), DEFAULT_MODEL
    elif prompt and not model:
        model = DEFAULT_MODEL
    await h.wizard(ctx, {"app": app, "ws": ws, "model": model, "prompt": prompt})


async def _sessions(h: Handlers, ctx: Context, cmd: Command) -> None:
    sessions = h.scope.keep(await h.service.sessions())
    if not sessions:
        await h.reply(ctx, "No active sessions.")
        return
    nonce = await card_state.issue_nonce(h.service.registry)
    await h.card(ctx, f"Sessions ({len(sessions)})", cards.dashboard(sessions, nonce=nonce))


def _visible(models: list[ModelInfo]) -> list[ModelInfo]:
    return [m for m in models if not cards.is_hidden_model(m.id, m.label)]


async def _models(h: Handlers, ctx: Context, cmd: Command) -> None:
    models = _visible(await h.service.models(cmd.args[0]))
    lines = [f"- `{m.id}` {m.label}{' (default)' if m.default else ''}" for m in models]
    await h.reply(ctx, "\n".join(lines) or "No models reported.")


async def _apply_model(h: Handlers, ctx: Context, session: SessionRecord, model: str) -> None:
    await h.service.set_model(session.session_key, model)
    await h.reply(ctx, f"Model set to `{model}`.")
    await h.threads.refresh_root(await h.service.session(session.session_key))


async def _model(h: Handlers, ctx: Context, cmd: Command) -> None:
    session = h.require(ctx, Capability.MODEL_SELECT, "model selection")
    if cmd.args:
        await _apply_model(h, ctx, session, cmd.args[0])
        return
    current = (await h.service.session(session.session_key)).model
    shown = current or "default"
    try:
        models = _visible(await h.service.models(session.harness.value))
    except RelayError as exc:
        await h.reply(ctx, f"Model: `{shown}` ({exc})")
        return
    if not models:
        await h.reply(ctx, f"Model: `{shown}`")
        return
    ids = [m.id for m in models]
    selected = current if current in ids else next((m.id for m in models if m.default), ids[0])
    choices = list({m.id: (m.id, m.label) for m in models}.values())
    nonce = await card_state.issue_nonce(h.service.registry)
    card = cards.model_switch(session.session_key, shown, choices, selected, nonce=nonce)
    await h.card(ctx, f"Model: `{shown}`", card)


async def _model_action(service: RelayService, handlers: Handlers, inputs: dict[str, JsonValue]) -> Outcome:
    key, model = inputs.get("session"), inputs.get("model")
    if not isinstance(key, str) or not isinstance(model, str) or not model:
        raise InvalidInput("Pick a model first.")
    ctx = Context(await service.session(key))
    session = handlers.require(ctx, Capability.MODEL_SELECT, "model selection")
    if model not in {m.id for m in _visible(await service.models(session.harness.value))}:
        raise InvalidInput(
            f"{session.harness.value} has no model `{model}`. Try `/models {session.harness.value}`."
        )
    await _apply_model(handlers, ctx, session, model)
    return "done"


async def _stop(h: Handlers, ctx: Context, cmd: Command) -> None:
    session = h.require(ctx, Capability.CANCEL, "cancel")
    await h.service.stop(session.session_key)
    await h.reply(ctx, "⏹️ Stopped.")


async def _status(h: Handlers, ctx: Context, cmd: Command) -> None:
    assert ctx.session is not None
    await h.reply(ctx, await h.service.status(ctx.session.session_key))


async def _send_diff(h: Handlers, ctx: Context, session_key: str) -> None:
    diff = await h.service.diff(session_key)
    if not diff.strip():
        await h.reply(ctx, "No changes.")
    else:
        await h.reply(ctx, "📎 changes.diff", file=("changes.diff", diff, "text/x-diff"))


async def _diff(h: Handlers, ctx: Context, cmd: Command) -> None:
    assert ctx.session is not None
    await _send_diff(h, ctx, ctx.session.session_key)


async def _sessions_action(
    service: RelayService, handlers: Handlers, inputs: dict[str, JsonValue]
) -> Outcome:
    key, op = inputs.get("session"), inputs.get("op")
    if op not in cards.DASHBOARD_OPS:
        raise InvalidInput("That is not an action this card offers.")
    if not isinstance(key, str) or not key:
        raise InvalidInput("Pick a session first.")
    ctx = Context(await service.session(key))
    match op:
        case "status":
            await handlers.reply(ctx, await service.status(key))
        case "diff":
            await _send_diff(handlers, ctx, key)
        case "stop":
            handlers.require(ctx, Capability.CANCEL, "cancel")
            await service.stop(key)
            await handlers.reply(ctx, "⏹️ Stopped.")
        case "back":
            await handlers.reply(ctx, await service.back(key))
        case _:
            await handlers.reply(ctx, await service.set_away(True, key))
    return "done"


register_action("sessions", _sessions_action, reusable=True)
register_action("model", _model_action)


register(
    CommandSpec(
        "new",
        _new,
        "start a session (missing pieces are asked for)",
        usage='/new [app] [ws] [model] ["prompt"]',
        parse=_parse_new,
        order=10,
    )
)
register(
    CommandSpec(
        "sessions",
        _sessions,
        "list sessions, with status, diff, stop, back and away buttons",
        aliases=("session",),
        max_args=0,
        order=11,
    )
)
register(
    CommandSpec(
        "models",
        _models,
        "list an app's models",
        usage="/models <app>",
        min_args=1,
        max_args=1,
        order=12,
    )
)
register(
    CommandSpec(
        "model",
        _model,
        "pick or set the session's model",
        scope="thread",
        usage="/model [model]",
        max_args=1,
        order=13,
    )
)
register(CommandSpec("stop", _stop, "cancel the running turn", scope="thread", order=14))
register(CommandSpec("status", _status, "show the session's state", scope="thread", order=15))
register(CommandSpec("diff", _diff, "send the workspace's git diff", scope="thread", order=16))
