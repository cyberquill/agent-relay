"""/new, /sessions, /status, /stop, /diff, /model and /models."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from agent_relay.core.service import resolve_app
from agent_relay.frontends.commands import Command, Invalid
from agent_relay.frontends.registry import CommandSpec, register
from agent_relay.frontends.webex import cards
from agent_relay.frontends.webex.handlers import DEFAULT_MODEL
from agent_relay.models.capabilities import Capability

if TYPE_CHECKING:
    from agent_relay.frontends.webex.handlers import Context, Handlers

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
    sessions = await h.service.sessions()
    lines = [f"- {cards.root_markdown(s)}" for s in sessions] or ["No active sessions."]
    await h.reply(ctx, "\n".join(lines))


async def _models(h: Handlers, ctx: Context, cmd: Command) -> None:
    models = await h.service.models(cmd.args[0])
    lines = [f"- `{m.id}` {m.label}{' (default)' if m.default else ''}" for m in models]
    await h.reply(ctx, "\n".join(lines) or "No models reported.")


async def _model(h: Handlers, ctx: Context, cmd: Command) -> None:
    session = h.require(ctx, Capability.MODEL_SELECT, "model selection")
    if cmd.args:
        await h.service.set_model(session.session_key, cmd.args[0])
        await h.reply(ctx, f"Model set to `{cmd.args[0]}`.")
        await h.renderer.refresh_root(await h.service.session(session.session_key))
    else:
        current = (await h.service.session(session.session_key)).model or "default"
        await h.reply(ctx, f"Model: `{current}`")


async def _stop(h: Handlers, ctx: Context, cmd: Command) -> None:
    session = h.require(ctx, Capability.CANCEL, "cancel")
    await h.service.stop(session.session_key)
    await h.reply(ctx, "⏹️ Stopped.")


async def _status(h: Handlers, ctx: Context, cmd: Command) -> None:
    assert ctx.session is not None
    await h.reply(ctx, await h.service.status(ctx.session.session_key))


async def _diff(h: Handlers, ctx: Context, cmd: Command) -> None:
    assert ctx.session is not None
    diff = await h.service.diff(ctx.session.session_key)
    if not diff.strip():
        await h.reply(ctx, "No changes.")
    else:
        await h.reply(ctx, "📎 changes.diff", file=("changes.diff", diff, "text/x-diff"))


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
        "list sessions",
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
        "show or set the session's model",
        scope="thread",
        usage="/model [model]",
        max_args=1,
        order=13,
    )
)
register(CommandSpec("stop", _stop, "cancel the running turn", scope="thread", order=14))
register(CommandSpec("status", _status, "show the session's state", scope="thread", order=15))
register(CommandSpec("diff", _diff, "send the workspace's git diff", scope="thread", order=16))
