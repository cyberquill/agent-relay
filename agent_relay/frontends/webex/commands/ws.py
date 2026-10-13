"""/ws: list workspaces and, once `ws.roots` is set, add a folder, clone a repo or add a repo to one."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import JsonValue

from agent_relay.core.service import WS_NOT_ENABLED, RelayService
from agent_relay.core.service.workspaces import Started
from agent_relay.frontends import card_state
from agent_relay.frontends.actions import Outcome, register_action
from agent_relay.frontends.card_state import InvalidInput
from agent_relay.frontends.commands import Command
from agent_relay.frontends.registry import CommandSpec, register
from agent_relay.frontends.webex.cards import ws as cards
from agent_relay.frontends.webex.handlers import Context, Handlers
from agent_relay.models.config import Workspace

if TYPE_CHECKING:
    from pathlib import Path

TOP = Context(None)
MAX_FIELD_CHARS = 300
NO_FOLDERS = "No folders to offer under the configured roots: none exist yet, or every one is already in use."


async def _nonces(service: RelayService, *ops: str) -> dict[str, str]:
    return {op: await card_state.issue_nonce(service.registry) for op in ops}


def _field(inputs: dict[str, JsonValue], key: str) -> str:
    value = inputs.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise InvalidInput("That card carried something unexpected.")
    if len(value) > MAX_FIELD_CHARS:
        raise InvalidInput(f"`{key}` is too long.")
    return value.strip()


def _where(entry: Workspace) -> str:
    return f"`{entry.file or entry.path}`"


async def _ws(h: Handlers, ctx: Context, cmd: Command) -> None:
    spaces = h.service.workspaces()
    listing = cards.listing(spaces)
    if ctx.session is not None or not h.service.ws_roots():
        hint = "" if ctx.session is not None else f"\n\n{WS_NOT_ENABLED}"
        await h.reply(ctx, f"{listing}{hint}")
        return
    ops = [cards.OP_ADD_PICK, cards.OP_CLONE_FORM, *([cards.OP_REPO_PICK] if spaces else [])]
    card = cards.menu(spaces, await _nonces(h.service, *ops))
    await h.card(ctx, f"**Workspaces**\n{listing}", card)


async def _finished(h: Handlers, entry: Workspace, headline: str) -> None:
    nonce = await card_state.issue_nonce(h.service.registry)
    text = f"✅ {headline}\n{cards.start_hint(entry.name)}"
    await h.card(TOP, text, cards.done(entry.name, headline, nonce=nonce))


def _roots(service: RelayService) -> list[Path]:
    roots = service.ws_roots()
    if not roots:
        raise InvalidInput(WS_NOT_ENABLED)
    return roots


def _workspace(service: RelayService, inputs: dict[str, JsonValue]) -> str:
    name = _field(inputs, "workspace")
    if not any(w.name == name for w in service.workspaces()):
        raise InvalidInput(f"Unknown workspace `{name}`. Try `/ws`.")
    return name


async def _pick_folder(h: Handlers, inputs: dict[str, JsonValue], *, workspace: str | None) -> None:
    offer = await h.service.ws_folders(workspace)
    if not offer.choices:
        await h.reply(TOP, NO_FOLDERS)
        return
    nonce = await card_state.issue_nonce(h.service.registry)
    if workspace is None:
        card = cards.pick_folder(
            offer, title="Add a folder", op=cards.OP_ADD, button="Add", nonce=nonce, ask_name=True
        )
    else:
        card = cards.pick_folder(
            offer,
            title=f"Add a folder to {workspace}",
            op=cards.OP_REPO_ADD_FOLDER,
            button="Add to workspace",
            nonce=nonce,
            carry={"workspace": workspace},
        )
    await h.card(TOP, "Pick a folder", card)


async def _clone_form(h: Handlers, *, workspace: str | None) -> None:
    roots = _roots(h.service)
    nonce = await card_state.issue_nonce(h.service.registry)
    if workspace is None:
        card = cards.clone_form(roots, title="Clone a repo", op=cards.OP_CLONE, nonce=nonce, ask_name=True)
    else:
        card = cards.clone_form(
            roots,
            title=f"Clone a repo into {workspace}",
            op=cards.OP_REPO_ADD_CLONE,
            nonce=nonce,
            carry={"workspace": workspace},
        )
    await h.card(TOP, "Clone a repo", card)


def _cloning(h: Handlers) -> Started:
    async def started(repo: str) -> None:
        await h.reply(TOP, f"⏳ Cloning `{repo}`. This can take a while.")

    return started


async def _act(service: RelayService, h: Handlers, inputs: dict[str, JsonValue]) -> Outcome:
    op = inputs.get("op")
    if op == cards.OP_ADD_PICK:
        await _pick_folder(h, inputs, workspace=None)
    elif op == cards.OP_ADD:
        entry = await service.ws_add_folder(inputs.get("folder"), _field(inputs, "name") or None)
        await _finished(h, entry, f"Added workspace **{entry.name}** at {_where(entry)}.")
    elif op == cards.OP_CLONE_FORM:
        await _clone_form(h, workspace=None)
    elif op == cards.OP_CLONE:
        entry = await service.ws_clone(
            _field(inputs, "url"), _field(inputs, "name") or None, inputs.get("root"), _cloning(h)
        )
        await _finished(h, entry, f"Cloned and added workspace **{entry.name}** at {_where(entry)}.")
    elif op == cards.OP_REPO_PICK:
        spaces = service.workspaces()
        if not spaces:
            await h.reply(TOP, "No workspaces yet. Add a folder or clone a repo first.")
            return "done"
        nonces = await _nonces(service, cards.OP_REPO_FOLDER, cards.OP_REPO_CLONE)
        card = cards.pick_workspace(
            spaces, folder_nonce=nonces[cards.OP_REPO_FOLDER], clone_nonce=nonces[cards.OP_REPO_CLONE]
        )
        await h.card(TOP, "Pick a workspace", card)
    elif op == cards.OP_REPO_FOLDER:
        await _pick_folder(h, inputs, workspace=_workspace(service, inputs))
    elif op == cards.OP_REPO_CLONE:
        await _clone_form(h, workspace=_workspace(service, inputs))
    elif op == cards.OP_REPO_ADD_FOLDER:
        entry = await service.ws_add_repo_folder(_workspace(service, inputs), inputs.get("folder"))
        await _finished(h, entry, f"Added the folder to workspace **{entry.name}** ({_where(entry)}).")
    elif op == cards.OP_REPO_ADD_CLONE:
        entry = await service.ws_add_repo_clone(
            _workspace(service, inputs), _field(inputs, "url"), inputs.get("root"), _cloning(h)
        )
        await _finished(h, entry, f"Cloned the repo into workspace **{entry.name}** ({_where(entry)}).")
    else:
        raise InvalidInput("That is not an action this card offers.")
    return "done"


# A clone may have run before a later step failed, so a failure past validation is not retried.
register(CommandSpec("ws", _ws, "list and add workspaces", aliases=("workspaces",), max_args=0, order=31))
register_action(cards.KIND, _act, retryable=False)
