"""VS Code's own agent chats ("Local"): `workspaceStorage/<hash>/chatSessions/<id>.jsonl`.

A file is one snapshot line followed by patches (`kind` 1 sets a value at a path `k`, `kind` 2 appends
to a list). Files reach tens of MB, so only the tail is replayed: enough for the title, the last
prompt and the last reply. `<hash>/workspace.json` names the folder. Multi-root windows (a
`workspace` key) are not matched to a registered folder and count as outside the workspaces.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

from pydantic import JsonValue

from agent_relay import paths
from agent_relay.core.chats import text as t
from agent_relay.core.chats.model import DeskChat
from agent_relay.models.events import Harness

RUNNING_WINDOW_S = 120.0


def _folder(storage: Path) -> str:
    try:
        meta = json.loads((storage / "workspace.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    uri = meta.get("folder") if isinstance(meta, dict) else None
    parsed = urlparse(uri) if isinstance(uri, str) else None
    return unquote(parsed.path) if parsed is not None and parsed.scheme == "file" else ""


def _request_text(request: JsonValue) -> str:
    message = request.get("message") if isinstance(request, dict) else None
    value = message.get("text") if isinstance(message, dict) else None
    return value if isinstance(value, str) else ""


def _markdown(parts: JsonValue) -> list[str | None]:
    """Each response part as its text, or None for a tool call (which ends what came before)."""
    found: list[str | None] = []
    for part in parts if isinstance(parts, list) else []:
        if not isinstance(part, dict):
            continue
        kind = part.get("kind")
        if kind == "toolInvocationSerialized":
            found.append(None)
        elif kind is None and isinstance(part.get("value"), str):
            found.append(str(part["value"]))
    return found


def _answer(pieces: list[str | None]) -> str | None:
    """The text after the last tool call: earlier text was narration."""
    kept: list[str] = []
    for piece in pieces:
        if piece is None:
            kept.clear()
        elif piece.strip():
            kept.append(piece)
    return "".join(kept).strip() or None


class _Replay:
    """The last request of a chat, rebuilt from the patches in the window."""

    def __init__(self) -> None:
        self.title: str | None = None
        self.prompt: str = ""
        self.pieces: list[str | None] = []
        self.settled = False
        self._last: int | None = None

    def feed(self, entry: JsonValue) -> None:
        if not isinstance(entry, dict):
            return
        kind, key, value = entry.get("kind"), entry.get("k"), entry.get("v")
        if kind == 0 and isinstance(value, dict):
            self._snapshot(value)
        elif key == ["customTitle"] and isinstance(value, str):
            self.title = value
        elif kind == 2 and key == ["requests"] and isinstance(value, list) and value:
            self._begin(value[-1], None)
        elif (
            isinstance(key, list)
            and len(key) == 3
            and key[0] == "requests"
            and isinstance(key[1], int)
            and isinstance(key[2], str)
        ):
            self._patch(key[1], key[2], kind, value)

    def _snapshot(self, value: dict[str, JsonValue]) -> None:
        title = value.get("customTitle")
        self.title = title if isinstance(title, str) else self.title
        requests = value.get("requests")
        if isinstance(requests, list) and requests:
            self._begin(requests[-1], len(requests) - 1)
            state = requests[-1].get("modelState") if isinstance(requests[-1], dict) else None
            self.settled = isinstance(state, dict) and "completedAt" in state

    def _begin(self, request: JsonValue, index: int | None) -> None:
        self.prompt = _request_text(request)
        self.pieces = _markdown(request.get("response")) if isinstance(request, dict) else []
        self.settled = False
        self._last = index

    def _patch(self, index: int, field: str, kind: object, value: JsonValue) -> None:
        if self._last is None or index > self._last:
            self._last = index
        if index != self._last:
            return
        if field == "response":
            parts = _markdown(value)
            self.pieces = parts if kind == 1 else [*self.pieces, *parts]
            self.settled = False
        elif field == "modelState" and isinstance(value, dict) and "completedAt" in value:
            self.settled = True


def _chat(file: Path, workspace: str, now: float) -> DeskChat | None:
    try:
        modified = file.stat().st_mtime
    except OSError:
        return None
    replay = _Replay()
    for entry in t.tail_lines(file):
        replay.feed(entry)
    first = t.user_text(replay.prompt)
    return DeskChat(
        app=Harness.VSCODE,
        native_id=file.stem,
        workspace=workspace,
        title=t.safe(t.short(replay.title or first, t.TITLE_CHARS)) or "(no prompt)",
        modified=datetime.fromtimestamp(modified, UTC),
        last_prompt=t.safe(first) or None,
        last_reply=t.safe(_answer(replay.pieces)),
        running=now - modified < RUNNING_WINDOW_S and not replay.settled,
        transcript=str(file),
    )


def read(since: float, roots: Sequence[Path], now: float) -> list[DeskChat]:
    base = paths.vscode_workspace_storage()
    if not base.is_dir():
        return []
    found: list[DeskChat] = []
    for storage in base.iterdir():
        folder = _folder(storage)
        for file in (storage / "chatSessions").glob("*.jsonl"):
            try:
                if file.stat().st_mtime < since:
                    continue
            except OSError:
                continue
            chat = _chat(file, folder, now)
            if chat is not None:
                found.append(chat)
    return found
