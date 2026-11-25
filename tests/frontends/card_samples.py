"""Every card builder in `agent_relay/frontends/webex/cards/`, called with the largest input it takes.

Shared by `test_card_spec.py` (offline subset check) and `scripts/card_check.py` (live post to Webex).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import JsonValue

from agent_relay.core.chats import ChatPage, DeskChat
from agent_relay.core.workspaces import FolderChoice, FolderOffer
from agent_relay.frontends.webex import cards
from agent_relay.frontends.webex.cards import ws
from agent_relay.frontends.webex.cards.queue import MAX_ITEMS, MODE_LABELS, queue_card
from agent_relay.models.config import Workspace
from agent_relay.models.events import Harness
from agent_relay.models.sessions import SessionRecord, SessionStatus

NONCE = "n" * 12


def agent_entries(count: int = cards.AGENT_LIMIT) -> list[tuple[str, str]]:
    harnesses = [h.value for h in Harness]
    return [
        (f"{harnesses[i % len(harnesses)]}:model-{i}", f"⬛ Cursor IDE (hooks) · {'Long model name ' * 4}{i}")
        for i in range(count)
    ]


def workspace_entries(count: int = 20) -> list[tuple[str, str]]:
    return [
        (f"workspace-{i}", f"workspace-{i} · ~/Desktop/Repository/Personal/{'deep/' * 8}project-{i}")
        for i in range(count)
    ]


def _sessions(count: int) -> list[SessionRecord]:
    harnesses = list(Harness)
    return [
        SessionRecord(
            session_key=f"{harnesses[i % len(harnesses)].value}:s{i}",
            harness=harnesses[i % len(harnesses)],
            harness_id=f"s{i}",
            workspace=f"/Users/me/repo-{i}",
            title=f"A fairly long session title that goes on and on, number {i}" * 2,
            status=list(SessionStatus)[i % len(SessionStatus)],
        )
        for i in range(count)
    ]


def _chat_page() -> ChatPage:
    harnesses = list(Harness)
    now = datetime.now(UTC)
    chats = [
        DeskChat(
            app=harnesses[i % len(harnesses)],
            native_id=f"c{i}",
            workspace=f"/Users/me/{'deep/' * 6}repo-{i}",
            title="A long chat title that goes on " * 6,
            modified=now - timedelta(hours=i * 7),
            last_prompt="What the person asked, at length. " * 20,
            last_reply="What the agent answered, at length. " * 60,
            running=i % 3 == 0,
        )
        for i in range(10)
    ]
    return ChatPage(chats=chats, outside_workspaces=12, more=30)


def maximal_cards() -> dict[str, dict[str, JsonValue]]:
    """Name -> card, each built with maximal inputs (40 agents, 20+ workspaces, long texts)."""
    workspaces = workspace_entries(30)
    offer = FolderOffer(
        choices=tuple(
            FolderChoice(Path(f"/Users/me/Code/project-{i}"), f"project-{i} (git)", True) for i in range(25)
        ),
        total=80,
    )
    spaces = [Workspace(name=f"workspace-{i}", path=f"/Users/me/{'deep/' * 6}repo-{i}") for i in range(20)]
    items = [
        (i, "A queued message that is much longer than the card will show. " * 6)
        for i in range(MAX_ITEMS + 3)
    ]
    nonces = {"mode": NONCE, "clear": NONCE, **{f"remove:{i}": NONCE for i, _ in items}}
    ws_nonces = {ws.OP_ADD_PICK: NONCE, ws.OP_CLONE_FORM: NONCE, ws.OP_REPO_PICK: NONCE}
    return {
        "new_session": cards.new_session(
            workspaces,
            workspaces[3][0],
            agent_entries(),
            agent_entries()[7][0],
            "p" * (cards.MAX_PROMPT_CHARS // 10),
            nonce=NONCE,
            unavailable=[f"App {i} (down)" for i in range(6)],
            files=3,
        ),
        "new_session_unfilled": cards.new_session(
            workspaces[:1], None, agent_entries(1), None, None, nonce=NONCE
        ),
        "model_switch": cards.model_switch(
            "opencode:s1",
            "a-model",
            [(f"model-{i}", f"Model number {i} " * 6) for i in range(60)],
            "model-3",
            nonce=NONCE,
        ),
        "dashboard": cards.dashboard(_sessions(40), nonce=NONCE),
        "chats": cards.chats(_chat_page(), nonce=NONCE),
        "panic_confirm": cards.panic_confirm(nonce=NONCE),
        "question": cards.question(
            "q1",
            "Copilot · a long label " * 5,
            "Which database should the migration target? " * 80,
            tuple(f"Option {i} " + "x" * 80 for i in range(20)),
            nonce=NONCE,
        ),
        "question_free_text": cards.question("q2", "label", "Free text?", (), nonce=NONCE),
        "queue": queue_card("opencode:s1", next(iter(MODE_LABELS)), items, nonces=nonces),
        "queue_empty": queue_card("opencode:s1", "steer", [], nonces=nonces),
        "ws_menu": ws.menu(spaces, ws_nonces),
        "ws_pick_folder": ws.pick_folder(
            offer, title="Add a folder", op=ws.OP_ADD, button="Add", nonce=NONCE, ask_name=True
        ),
        "ws_clone_form": ws.clone_form(
            [Path("/Users/me/Code"), Path("/Users/me/Work")],
            title="Clone a repo",
            op=ws.OP_CLONE,
            nonce=NONCE,
            ask_name=True,
        ),
        "ws_pick_workspace": ws.pick_workspace(spaces, folder_nonce=NONCE, clone_nonce=NONCE),
        "ws_done": ws.done("workspace-1", "Added workspace-1", nonce=NONCE),
    }
