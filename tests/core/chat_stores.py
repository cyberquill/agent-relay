"""Fake app stores for the chat catalog tests: each builder writes what the app writes, under `RELAY_HOME`."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

from pydantic import JsonValue

from agent_relay import paths
from agent_relay.core.chats import cursor


def age(file: Path, seconds: float) -> None:
    """Make a file look `seconds` old."""
    stamp = time.time() - seconds
    os.utime(file, (stamp, stamp))


def write_lines(file: Path, rows: list[JsonValue]) -> Path:
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return file


def cursor_entry(role: str, *parts: JsonValue) -> dict[str, JsonValue]:
    return {"role": role, "message": {"content": list(parts)}}


def said(text: str) -> dict[str, JsonValue]:
    return {"type": "text", "text": text}


TOOL: dict[str, JsonValue] = {"type": "tool_use", "name": "Shell", "input": {"command": "ls"}}


def cursor_chat(
    workspace: Path,
    chat_id: str,
    prompt: str = "Add a parser test",
    reply: str = "Done: the parser has a test.",
    *,
    finished: bool = True,
    seconds_old: float = 3600,
    in_cli: bool = False,
) -> Path:
    slug = cursor.slug(workspace.resolve())
    rows: list[JsonValue] = [
        cursor_entry("user", said(f"<timestamp>Mon</timestamp>\n<user_query>\n{prompt}\n</user_query>")),
        cursor_entry("assistant", said("Looking at the repo."), TOOL),
    ]
    if finished:
        rows.append(cursor_entry("assistant", said(reply)))
    file = write_lines(
        paths.cursor_projects() / slug / "agent-transcripts" / chat_id / f"{chat_id}.jsonl", rows
    )
    age(file, seconds_old)
    if in_cli:
        folder = paths.cursor_cli_chats() / "hash" / chat_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "meta.json").write_text("{}", encoding="utf-8")
    return file


def copilot_chat(
    workspace: Path,
    chat_id: str,
    prompt: str = "Run the triage skill",
    reply: str = "Triage is done.",
    *,
    host: bool = True,
    name: str | None = None,
    finished: bool = True,
    seconds_old: float = 3600,
) -> Path:
    folder = paths.copilot_session_state() / chat_id
    folder.mkdir(parents=True, exist_ok=True)
    meta = [f"id: {chat_id}", f"cwd: {workspace}"]
    if host:
        meta.append("client_name: vscode-agent-host")
    if name is not None:
        meta.append(f"name: {name}")
    stamp = datetime.fromtimestamp(time.time() - seconds_old, UTC).isoformat().replace("+00:00", "Z")
    meta.append(f"updated_at: {stamp}")
    (folder / "workspace.yaml").write_text("\n".join(meta) + "\n", encoding="utf-8")
    events: list[JsonValue] = [
        {"type": "user.message", "data": {"content": prompt}},
        {"type": "assistant.message", "data": {"content": "Checking.", "toolRequests": [{"id": "t1"}]}},
        {"type": "hook.start", "data": {}},
    ]
    if finished:
        events += [
            {"type": "assistant.message", "data": {"content": reply}},
            {"type": "assistant.turn_end", "data": {}},
            {"type": "hook.end", "data": {}},
            {"type": "session.usage_checkpoint", "data": {}},
        ]
    file = write_lines(folder / "events.jsonl", events)
    age(file, seconds_old)
    return file


def vscode_chat(
    workspace: Path,
    chat_id: str,
    prompt: str = "Explain the router",
    reply: str = "The router fans events out.",
    *,
    title: str | None = None,
    finished: bool = True,
    seconds_old: float = 3600,
    storage: str = "abc123",
) -> Path:
    root = paths.vscode_workspace_storage() / storage
    root.mkdir(parents=True, exist_ok=True)
    (root / "workspace.json").write_text(
        json.dumps({"folder": workspace.resolve().as_uri()}), encoding="utf-8"
    )
    request: dict[str, JsonValue] = {"message": {"text": prompt}, "response": []}
    rows: list[JsonValue] = [
        {"kind": 0, "v": {"sessionId": chat_id, "requests": [request], "creationDate": 1}},
        {"kind": 2, "k": ["requests", 0, "response"], "v": [{"value": "Let me look."}]},
        {"kind": 2, "k": ["requests", 0, "response"], "v": [{"kind": "toolInvocationSerialized"}]},
        {"kind": 2, "k": ["requests", 0, "response"], "v": [{"value": reply}]},
    ]
    if title:
        rows.append({"kind": 1, "k": ["customTitle"], "v": title})
    if finished:
        rows.append({"kind": 1, "k": ["requests", 0, "modelState"], "v": {"value": 1, "completedAt": 5}})
    file = write_lines(root / "chatSessions" / f"{chat_id}.jsonl", rows)
    age(file, seconds_old)
    return file


def opencode_db(
    workspace: Path,
    chat_id: str,
    prompt: str = "Send the report",
    reply: str = "Sent.",
    *,
    finished: bool = True,
    seconds_old: float = 3600,
    parent: str | None = None,
) -> Path:
    file = paths.opencode_db()
    file.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(file)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS session (id TEXT PRIMARY KEY, parent_id TEXT, directory TEXT,
          title TEXT, time_created INTEGER, time_updated INTEGER, time_archived INTEGER);
        CREATE TABLE IF NOT EXISTS message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER,
          time_updated INTEGER, data TEXT);
        CREATE TABLE IF NOT EXISTS part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
          time_created INTEGER, time_updated INTEGER, data TEXT);
        """
    )
    updated = int((time.time() - seconds_old) * 1000)
    db.execute(
        "INSERT INTO session VALUES (?, ?, ?, ?, ?, ?, NULL)",
        (chat_id, parent, str(workspace), prompt[:40], updated, updated),
    )
    done: dict[str, JsonValue] = {"created": updated - 1}
    if finished:
        done["completed"] = updated
    messages: list[tuple[str, int, dict[str, JsonValue], str]] = [
        (f"{chat_id}-u", updated - 2, {"role": "user"}, prompt),
        (f"{chat_id}-a", updated - 1, {"role": "assistant", "time": done}, reply),
    ]
    for mid, stamp, data, body in messages:
        db.execute(
            "INSERT INTO message VALUES (?, ?, ?, ?, ?)", (mid, chat_id, stamp, stamp, json.dumps(data))
        )
        db.execute(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
            (f"{mid}-p", mid, chat_id, stamp, stamp, json.dumps({"type": "text", "text": body})),
        )
    db.commit()
    db.close()
    return file
