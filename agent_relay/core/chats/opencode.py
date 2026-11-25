"""OpenCode chats: the `session`, `message` and `part` tables of `opencode.db`, opened read-only.

Sub-agent sessions (a `parent_id`) and archived ones are left out. Times are milliseconds.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import JsonValue

from agent_relay import paths
from agent_relay.core.chats import text as t
from agent_relay.core.chats.model import DeskChat
from agent_relay.log import get_logger
from agent_relay.models.events import Harness

log = get_logger(__name__)

RUNNING_WINDOW_S = 120.0
BUSY_TIMEOUT_MS = 1500

_SESSIONS = (
    "SELECT id, title, directory, time_updated FROM session "
    "WHERE parent_id IS NULL AND time_archived IS NULL AND time_updated >= ?"
)
_LAST_MESSAGE = (
    "SELECT id, data FROM message WHERE session_id = ? AND json_extract(data, '$.role') = ? "
    "ORDER BY time_created DESC, id DESC LIMIT 1"
)
_TEXT_PARTS = (
    "SELECT json_extract(data, '$.text') FROM part "
    "WHERE message_id = ? AND json_extract(data, '$.type') = 'text' ORDER BY id"
)


def _connect(file: Path) -> sqlite3.Connection:
    db = sqlite3.connect(f"{file.as_uri()}?mode=ro", uri=True, timeout=BUSY_TIMEOUT_MS / 1000)
    db.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return db


def _text_of(db: sqlite3.Connection, message_id: str) -> str:
    rows = db.execute(_TEXT_PARTS, (message_id,)).fetchall()
    return "\n".join(str(row[0]) for row in rows if isinstance(row[0], str) and row[0].strip())


def _last(db: sqlite3.Connection, session: str, role: str) -> tuple[str, dict[str, JsonValue]] | None:
    row = db.execute(_LAST_MESSAGE, (session, role)).fetchone()
    if row is None:
        return None
    try:
        data = json.loads(row[1])
    except json.JSONDecodeError:
        return None
    return (str(row[0]), data) if isinstance(data, dict) else None


def _completed(data: dict[str, JsonValue]) -> bool:
    stamps = data.get("time")
    return isinstance(stamps, dict) and stamps.get("completed") is not None


def _chat(db: sqlite3.Connection, row: tuple[str, str, str, int], now: float) -> DeskChat:
    sid, title, directory, updated_ms = row
    user = _last(db, sid, "user")
    reply = _last(db, sid, "assistant")
    prompt = _text_of(db, user[0]) if user else ""
    answer = _text_of(db, reply[0]) if reply else ""
    modified = updated_ms / 1000
    return DeskChat(
        app=Harness.OPENCODE,
        native_id=sid,
        workspace=directory,
        title=t.safe(t.short(title or prompt, t.TITLE_CHARS)) or "(no prompt)",
        modified=datetime.fromtimestamp(modified, UTC),
        last_prompt=t.safe(prompt) or None,
        last_reply=t.safe(answer) or None,
        running=now - modified < RUNNING_WINDOW_S and (reply is None or not _completed(reply[1])),
    )


def read(since: float, roots: Sequence[Path], now: float) -> list[DeskChat]:
    file = paths.opencode_db()
    if not file.is_file():
        return []
    try:
        db = _connect(file)
    except sqlite3.Error as exc:
        log.debug(f"OpenCode chats unavailable: {exc}")
        return []
    try:
        rows = db.execute(_SESSIONS, (int(since * 1000),)).fetchall()
        return [_chat(db, (str(r[0]), str(r[1]), str(r[2]), int(r[3])), now) for r in rows]
    except sqlite3.Error as exc:
        log.debug(f"OpenCode chats unavailable: {exc}")
        return []
    finally:
        db.close()
