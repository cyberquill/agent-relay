"""Last assistant reply from Copilot (JSON Lines and JSON) and Claude-style transcripts."""

from __future__ import annotations

import asyncio
import json
import time
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


def test_an_agent_host_transcript_names_its_client_and_a_copilot_cli_one_does_not(tmp_path: Path) -> None:
    def events(name: str, metadata: object, content: str = "You are Copilot.") -> str:
        file = tmp_path / f"{name}.jsonl"
        rows = [
            {"type": "session.start", "data": {"sessionId": name, "producer": "copilot-agent"}},
            {"type": "user.message", "data": {"content": "hi"}},
            {"type": "system.message", "data": {"content": content, "metadata": metadata}},
        ]
        file.write_text("\n".join(json.dumps(r, separators=(",", ":")) for r in rows))
        return str(file)

    trailer = {"variables": {"gitCommitTrailer": {"clientName": "vscode-agent-host"}}}
    assert transcript.from_agent_host(events("host", trailer))
    assert not transcript.from_agent_host(events("cli", None))
    quoted = events("quoted", None, content='say "clientName":"vscode-agent-host"')
    assert not transcript.from_agent_host(quoted), "text in a message is escaped, never a key"
    assert not transcript.from_agent_host(str(tmp_path / "nope.jsonl"))
    assert not transcript.from_agent_host(None)


def test_copilot_model_call_telemetry_is_never_the_reply(tmp_path: Path) -> None:
    """The stop hook can run before the final `assistant.message` is on disk; the frustration classifier's
    `model.response` (role assistant) is then the newest assistant-looking entry and must be ignored."""
    file = tmp_path / "events.jsonl"
    classifier = {"content": '{"frustration":false,"confidence":"high"}', "role": "assistant"}
    events = [
        {"type": "assistant.message", "data": {"content": "Earlier answer"}},
        {"type": "user.message", "data": {"content": "again"}},
        {"type": "model.response", "data": {"kind": "response", "response": classifier}},
        {"type": "model.messages_snapshot", "data": {"messages": [classifier]}},
        {"type": "hook.start", "data": {"hookType": "agentStop"}},
    ]
    file.write_text("\n".join(json.dumps(e) for e in events))
    assert transcript.last_reply(str(file)) is None  # not this turn's answer: the caller uses the stream
    only = tmp_path / "first.jsonl"
    only.write_text("\n".join(json.dumps(e) for e in events[2:]))
    assert transcript.last_reply(str(only)) is None


def test_narration_before_a_tool_call_is_not_the_reply_when_the_closing_message_is_not_on_disk(
    tmp_path: Path,
) -> None:
    """Seen live (2026-11-22): the Agent Host's stop hook ran before the closing `assistant.message` reached
    `events.jsonl`, and the phone got "Ledger written. Checking X." (the message before a tool call) instead
    of the 7,000-character triage."""
    file = tmp_path / "events.jsonl"
    events = [
        {"type": "user.message", "data": {"content": "triage"}},
        {"type": "assistant.message", "data": {"content": "Looking.", "toolRequests": [{"name": "bash"}]}},
        {"type": "tool.execution_complete", "data": {"toolCallId": "t1"}},
        {"type": "assistant.message", "data": {"content": "Ledger written. Checking.", "toolRequests": [{}]}},
        {"type": "tool.execution_complete", "data": {"toolCallId": "t2"}},
    ]
    file.write_text("\n".join(json.dumps(e) for e in events))
    assert transcript.last_reply(str(file)) is None
    closing = {"type": "assistant.message", "data": {"content": "Full triage.", "toolRequests": []}}
    file.write_text("\n".join(json.dumps(e) for e in [*events, closing]))
    assert transcript.last_reply(str(file)) == "Full triage."


async def test_wait_for_reply_waits_for_the_closing_message_behind_narration(tmp_path: Path) -> None:
    file = tmp_path / "events.jsonl"
    narration = {"type": "assistant.message", "data": {"content": "Checking.", "toolRequests": [{}]}}
    file.write_text(json.dumps({"type": "user.message", "data": {"content": "go"}}) + "\n")
    with file.open("a") as handle:
        handle.write(json.dumps(narration) + "\n")

    async def write_late() -> None:
        await asyncio.sleep(0.3)
        closing = {"type": "assistant.message", "data": {"content": "The answer.", "toolRequests": []}}
        with file.open("a") as handle:
            handle.write(json.dumps(closing) + "\n")

    writer = asyncio.create_task(write_late())
    assert await transcript.wait_for_reply(str(file), wait_s=3.0, poll_s=0.05) == "The answer."
    await writer


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


def test_only_the_text_after_the_last_tool_call_is_the_reply(tmp_path: Path) -> None:
    jsonl = tmp_path / "t.jsonl"
    entry = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Let me look."},
                {"type": "tool_use", "name": "bash"},
                {"type": "text", "text": "Now the second look."},
                {"type": "tool_use", "name": "bash"},
                {"type": "text", "text": "All done: 4 files."},
            ],
        },
    }
    jsonl.write_text(json.dumps(entry))
    assert transcript.last_reply(str(jsonl)) == "All done: 4 files."
    chat = tmp_path / "t.json"
    session = {
        "requests": [
            {
                "message": {"text": "go"},
                "response": [
                    {"value": "Checking."},
                    {"kind": "toolInvocation"},
                    {"value": "Checked: all fine."},
                ],
            }
        ]
    }
    chat.write_text(json.dumps(session))
    assert transcript.last_reply(str(chat)) == "Checked: all fine."


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


async def test_wait_for_reply_sees_a_reply_written_after_the_hook_ran(tmp_path: Path) -> None:
    file = tmp_path / "events.jsonl"
    file.write_text(json.dumps({"type": "user.message", "data": {"content": "hi"}}) + "\n")

    async def write_late() -> None:
        await asyncio.sleep(0.3)
        with file.open("a") as handle:
            handle.write(json.dumps({"type": "assistant.message", "data": {"content": "Late"}}) + "\n")

    writer = asyncio.create_task(write_late())
    assert await transcript.wait_for_reply(str(file), wait_s=3.0, poll_s=0.05) == "Late"
    await writer


async def test_wait_for_reply_gives_up_and_does_not_wait_for_a_missing_file(tmp_path: Path) -> None:
    file = tmp_path / "events.jsonl"
    file.write_text(json.dumps({"type": "user.message", "data": {"content": "hi"}}) + "\n")
    assert await transcript.wait_for_reply(str(file), wait_s=0.2, poll_s=0.05) is None
    started = time.monotonic()
    assert await transcript.wait_for_reply(str(tmp_path / "nope.jsonl"), wait_s=5.0) is None
    assert await transcript.wait_for_reply(None) is None
    assert time.monotonic() - started < 1.0
