"""The `/ws` cards: the menu, the folder and clone forms, the workspace picker and the hand-off to `/new`.

Every submit carries `relay: "ws"`, an `op` saying which step it is, and a one-use `nonce`.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from pydantic import JsonValue

from agent_relay.core.workspaces import FolderOffer
from agent_relay.frontends.webex.cards.common import card, choice_set, submit, text
from agent_relay.models.config import Workspace

KIND = "ws"
MAX_URL_CHARS = 300
MAX_NAME_CHARS = 40

OP_ADD_PICK = "add_pick"
OP_ADD = "add"
OP_CLONE_FORM = "clone_form"
OP_CLONE = "clone"
OP_REPO_PICK = "repo_pick"
OP_REPO_FOLDER = "repo_folder"
OP_REPO_CLONE = "repo_clone"
OP_REPO_ADD_FOLDER = "repo_add_folder"
OP_REPO_ADD_CLONE = "repo_add_clone"


def line_input(field: str, hint: str, max_length: int) -> JsonValue:
    return {"type": "Input.Text", "id": field, "placeholder": hint, "maxLength": max_length}


def _data(op: str, nonce: str, carry: dict[str, JsonValue] | None = None) -> dict[str, JsonValue]:
    return {"relay": KIND, "op": op, "nonce": nonce, **(carry or {})}


def listing(spaces: Sequence[Workspace]) -> str:
    lines = [f"- **{w.name}** `{w.file or w.path}`" for w in spaces]
    return "\n".join(lines) or "No workspaces. Add them with `relay ws add <name> <path>`."


def menu(spaces: Sequence[Workspace], nonces: dict[str, str]) -> dict[str, JsonValue]:
    """`nonces` holds one per button, so tapping one does not spend the others."""
    body: list[JsonValue] = [text("Workspaces", weight="Bolder")]
    body += [text(f"{w.name} · {w.file or w.path}", wrap=True) for w in spaces] or [
        text("No workspaces yet.", subtle=True)
    ]
    actions: list[JsonValue] = [
        submit("Add folder", _data(OP_ADD_PICK, nonces[OP_ADD_PICK])),
        submit("Clone repo", _data(OP_CLONE_FORM, nonces[OP_CLONE_FORM])),
    ]
    if OP_REPO_PICK in nonces:
        actions.append(submit("Add repo to workspace", _data(OP_REPO_PICK, nonces[OP_REPO_PICK])))
    return card(body, actions)


def _more(offer: FolderOffer) -> list[JsonValue]:
    if offer.total <= len(offer.choices):
        return []
    return [text(f"Showing {len(offer.choices)} of {offer.total}, git repos first.", subtle=True)]


def pick_folder(
    offer: FolderOffer,
    *,
    title: str,
    op: str,
    button: str,
    nonce: str,
    carry: dict[str, JsonValue] | None = None,
    ask_name: bool = False,
) -> dict[str, JsonValue]:
    choices = [(str(c.path), c.label) for c in offer.choices]
    body: list[JsonValue] = [
        text(title, weight="Bolder"),
        choice_set("folder", choices, "compact", choices[0][0] if choices else None),
        *_more(offer),
    ]
    if ask_name:
        body.append(line_input("name", "Name (default: the folder's name)", MAX_NAME_CHARS))
    return card(body, [submit(button, _data(op, nonce, carry), "positive")])


def clone_form(
    roots: Sequence[Path],
    *,
    title: str,
    op: str,
    nonce: str,
    carry: dict[str, JsonValue] | None = None,
    ask_name: bool = False,
) -> dict[str, JsonValue]:
    body: list[JsonValue] = [
        text(title, weight="Bolder"),
        line_input("url", "https://github.com/owner/repo", MAX_URL_CHARS),
    ]
    if ask_name:
        body.append(line_input("name", "Name (default: the repo's name)", MAX_NAME_CHARS))
    body.append(text("Clone into", subtle=True))
    body.append(choice_set("root", [(str(r), str(r)) for r in roots], "compact", str(roots[0])))
    return card(body, [submit("Clone", _data(op, nonce, carry), "positive")])


def pick_workspace(
    spaces: Sequence[Workspace], *, folder_nonce: str, clone_nonce: str
) -> dict[str, JsonValue]:
    choices = [(w.name, f"{w.name} · {w.file or w.path}") for w in spaces]
    body: list[JsonValue] = [
        text("Add a repo to which workspace?", weight="Bolder"),
        choice_set("workspace", choices, "compact", choices[0][0] if choices else None),
    ]
    return card(
        body,
        [
            submit("Pick a folder", _data(OP_REPO_FOLDER, folder_nonce), "positive"),
            submit("Clone a repo", _data(OP_REPO_CLONE, clone_nonce)),
        ],
    )


def start_hint(workspace: str) -> str:
    return f'Or type `/new <app> {workspace} "…"`.'


def done(workspace: str, headline: str, *, nonce: str) -> dict[str, JsonValue]:
    return card(
        [text(headline, weight="Bolder"), text(start_hint(workspace), subtle=True)],
        [
            submit(
                "Start a session here",
                {"relay": "new", "step": "ws", "value": workspace, "nonce": nonce},
                "positive",
            )
        ],
    )
