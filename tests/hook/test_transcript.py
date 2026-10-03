"""Last assistant reply from Copilot (JSON Lines and JSON) and Claude-style transcripts."""

from __future__ import annotations

import json
from pathlib import Path

from agent_relay.hook import transcript


def test_jsonl_last_assistant_text_skips_tool_parts(tmp_path: Path) -> None:
    file = tmp_path / "t.jsonl"
    lines = [
        {"type": "user", "message": {"role": "user", "content": "do it"}},
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "First."}]},
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "name": "bash"},
                    {"type": "text", "text": "Done, tests pass."},
                ],
            },
        },
        "not json at all",
    ]
    file.write_text("\n".join(json.dumps(x) if not isinstance(x, str) else x for x in lines))
    assert transcript.last_reply(str(file)) == "Done, tests pass."


def test_copilot_events_shape(tmp_path: Path) -> None:
    file = tmp_path / "events.jsonl"
    events = [
        {"type": "assistant.message", "data": {"content": "Earlier"}},
        {"type": "tool.execution_start", "data": {"toolName": "bash"}},
        {"type": "assistant.message", "data": {"content": "Latest answer"}},
    ]
    file.write_text("\n".join(json.dumps(e) for e in events))
    assert transcript.last_reply(str(file)) == "Latest answer"


def test_vscode_chat_session_json(tmp_path: Path) -> None:
    file = tmp_path / "t.json"
    session = {
        "version": 3,
        "requests": [
            {"message": {"text": "hi"}, "response": [{"value": "Hello"}]},
            {"message": {"text": "again"}, "response": [{"kind": "toolInvocation"}, {"value": "Second"}]},
        ],
    }
    file.write_text(json.dumps(session))
    assert transcript.last_reply(str(file)) == "Second"


def test_missing_empty_and_huge(tmp_path: Path) -> None:
    assert transcript.last_reply(None) is None
    assert transcript.last_reply(str(tmp_path / "nope")) is None
    empty = tmp_path / "e.jsonl"
    empty.write_text("")
    assert transcript.last_reply(str(empty)) is None
    big = tmp_path / "big.jsonl"
    filler = json.dumps({"role": "user", "content": "x" * 1000})
    with big.open("w") as handle:
        for _ in range(3000):
            handle.write(filler + "\n")
        handle.write(json.dumps({"role": "assistant", "content": "tail"}) + "\n")
    assert big.stat().st_size > transcript.MAX_READ_BYTES
    assert transcript.last_reply(str(big)) == "tail"
