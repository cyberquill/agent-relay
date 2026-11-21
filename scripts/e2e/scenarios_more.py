"""Scenarios 6 (model), 8 (away round trip) and 9 (panic)."""

from __future__ import annotations

import re

from scripts.e2e.cardkit import build_inputs, card_of, has_card
from scripts.e2e.env import OK_PROMPT, OK_WORD, Env, Thread
from scripts.e2e.model import Msg, ScenarioFailed, ScenarioSkipped
from scripts.e2e.scenarios import thread_of
from scripts.e2e.watch import Watch

MODEL_REPLY = re.compile(r"Model\s*→\s*`?([^`\s]+)`?")
MODEL_LISTED = re.compile(r"^- `([^`]+)`", re.M)
NO_MODEL_SELECT = re.compile(r"(?i)(not support|no model selection|cannot|can't|does not)")
RUNNING_ICON = "⏳"
PARKED_ICON = "🅿️"


async def command(env: Env, text: str, what: str) -> Msg:
    """Send a top-level command and return the relay's first reply."""
    top = await env.watch().start()
    await env.post(text)
    return await top.wait_for(lambda _m: True, env.settings.command_s, what)


async def _next_turn_uses_model(env: Env, thread: Thread, chosen: str) -> str:
    await env.root_text(thread.root, lambda text: chosen in text, f"the model {chosen}")

    async def send() -> Msg:
        return await env.post(OK_PROMPT, parent_id=thread.root.id)

    result = await env.turn(thread, send)
    if not OK_WORD.search(result.final.body):
        raise ScenarioFailed(f"the next turn is not OK: {env.summary(result.final)}")
    return f"Model → {chosen}, root shows it, the next turn answered"


async def _model_reply(env: Env, replies: Watch, chosen: str) -> None:
    reply = await replies.wait_for(
        lambda m: MODEL_REPLY.search(m.body) is not None, env.settings.command_s, "the Model → reply"
    )
    found = MODEL_REPLY.search(reply.body)
    if found is None or found.group(1) != chosen:
        raise ScenarioFailed(f"the reply names another model than {chosen}: {env.summary(reply)}")


async def model_switch(env: Env, app: str) -> str:
    """`/model` card in the thread, submitted with another model; then a turn."""
    thread = thread_of(env, app)
    replies = await env.watch(thread.root.id).start()
    await env.post("/model", parent_id=thread.root.id)
    card_msg = await replies.wait_for(lambda _m: True, env.settings.card_s, "the /model card")
    if not has_card(card_msg, "model"):
        if NO_MODEL_SELECT.search(card_msg.body):
            raise ScenarioSkipped(f"{app} has no model selection: {env.summary(card_msg)}")
        raise ScenarioFailed(f"/model did not post a model card: {env.summary(card_msg)}")
    view = card_of(card_msg)
    assert view is not None
    action = view.action(relay="model")
    spec = next((i for i in view.inputs if i.kind == "choice"), None)
    if action is None or spec is None:
        raise ScenarioFailed("the model card has no model list or no Apply action")
    others = [c for c in spec.choices if c.value != spec.value and c.value != "default"]
    if not others:
        raise ScenarioSkipped("the model card offers no model other than the current one")
    chosen = others[0].value
    await env.user.submit_card(card_msg.id, build_inputs(view, action, {spec.id: chosen}))
    await env.card_deleted(card_msg.id)
    await _model_reply(env, replies, chosen)
    return await _next_turn_uses_model(env, thread, chosen)


async def model_switch_typed(env: Env, app: str) -> str:
    """The typed form: `/models <app>` for a model id, then `/model <id>` in the thread."""
    thread = thread_of(env, app)
    listing = await command(env, f"/models {app}", "the /models reply")
    ids = [i for i in MODEL_LISTED.findall(listing.body) if i != "default"]
    if not ids:
        raise ScenarioSkipped(f"/models {app} lists no model to switch to: {env.summary(listing)}")
    chosen = ids[0]
    replies = await env.watch(thread.root.id).start()
    await env.post(f"/model {chosen}", parent_id=thread.root.id)
    await _model_reply(env, replies, chosen)
    return await _next_turn_uses_model(env, thread, chosen)


async def away_round_trip(env: Env, _app: str) -> str:
    """`/away`, a desk chat's turn (done by the human), a reply that continues it, `/back`."""
    top = await env.watch().start()
    sent_back = False
    await env.post("/away")
    try:
        ack = await top.wait_for(lambda _m: True, env.settings.command_s, "the /away reply")
        env.say(
            f"Away mode is on. Within {env.settings.away_wait_s:.0f}s, run one short chat turn in a desk app "
            f"(Cursor, VS Code or Copilot CLI) in .scratch, for example: {OK_PROMPT}"
        )
        try:
            root = await top.wait_for(
                lambda m: m.id != ack.id and not m.attachments,
                env.settings.away_wait_s,
                "a desk chat's thread",
            )
        except ScenarioFailed as exc:
            raise ScenarioSkipped(f"no desk chat turn within {env.settings.away_wait_s:.0f}s") from exc
        top.ignore(root.id)
        first = await env.watch(root.id).start(fresh=True)
        await first.wait_for(lambda _m: True, env.settings.turn_s, "the desk chat's final result")
        desk = Thread("desk", root)

        async def send() -> Msg:
            return await env.post(OK_PROMPT, parent_id=root.id)

        result = await env.turn(desk, send)
        if not OK_WORD.search(result.final.body):
            raise ScenarioFailed(f"the continued desk chat did not answer OK: {env.summary(result.final)}")
        sent_back = True
        await command(env, "/back", "the /back reply")
        await env.root_text(root, lambda text: PARKED_ICON not in text, "the desk chat released")
    finally:
        if not sent_back:
            await _best_effort_back(env)
    return "desk result in a new thread, a reply continued it, /back released it"


async def _best_effort_back(env: Env) -> None:
    try:
        await env.post("/back")
    except Exception as exc:
        env.say(f"could not send /back to leave away mode: {env.redact(str(exc))}")


async def _after_panic(env: Env) -> str:
    for thread in env.threads.values():
        await env.root_text(thread.root, lambda text: RUNNING_ICON not in text, "no running turn")
    await command(env, "/away", "the /away reply (resume)")
    await command(env, "/help", "the /help reply after resuming")
    await command(env, "/back", "the /back reply")
    return "everything stopped, /away resumed the relay, /back left away mode off"


async def panic(env: Env, _app: str) -> str:
    """`/panic`, the confirm card, then a check that nothing runs and the relay answers again."""
    top = await env.watch().start()
    await env.post("/panic")
    card_msg = await top.wait_for(lambda m: has_card(m, "panic"), env.settings.card_s, "the /panic card")
    top.ignore(card_msg.id)
    view = card_of(card_msg)
    action = view.action(relay="panic", op="confirm") if view else None
    if view is None or action is None:
        raise ScenarioFailed("the /panic card has no confirm action")
    await env.user.submit_card(card_msg.id, build_inputs(view, action))
    await env.card_deleted(card_msg.id)
    await top.wait_for(lambda _m: True, env.settings.command_s, "the /panic reply")
    return await _after_panic(env)


async def panic_typed(env: Env, _app: str) -> str:
    await command(env, "/panic now", "the /panic reply")
    return await _after_panic(env)
