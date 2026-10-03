"""Cursor CLI pieces in isolation: model parsing, ACP mapping and the stdio JSON-RPC transport."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

import pytest
from pydantic import JsonValue

from agent_relay.adapters.base import AdapterUnavailableError
from agent_relay.adapters.cursor_cli import mapping as m
from agent_relay.adapters.cursor_cli.adapter import CursorCliAdapter
from agent_relay.adapters.cursor_cli.catalog import CursorCli, needs_login, parse_models
from agent_relay.adapters.cursor_cli.rpc import (
    AcpExitedError,
    AcpProcess,
    AcpRpcError,
    MethodNotFoundError,
    Params,
)
from agent_relay.core.registry import Registry

# models


def test_parse_models_handles_the_common_shapes() -> None:
    text = (
        "\x1b[1mAvailable models\x1b[0m\n\nauto - Auto (current)\n"
        "* gpt-5: GPT-5\nsonnet-4.5-thinking\nTip: use --model\n"
    )
    models = parse_models(text)
    assert [(x.id, x.label, x.default) for x in models] == [
        ("auto", "Auto", True),
        ("gpt-5", "GPT-5", False),
        ("sonnet-4.5-thinking", "sonnet-4.5-thinking", False),
    ]


def test_parse_models_skips_prose_and_duplicates() -> None:
    assert parse_models(
        "Loading models...\nThese are your models:\ngpt-5 - A\ngpt-5 - B\nnot a model line here\n"
    ) == [parse_models("gpt-5 - A")[0]]


def test_login_markers() -> None:
    assert needs_login("Error: Authentication required. Run 'agent login'")
    assert needs_login("Not logged in")
    assert not needs_login("Logged in as dev@example.com")


async def test_cli_status_is_cached_and_missing_cli_is_reported(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    now = [0.0]

    async def runner(argv: Sequence[str]) -> tuple[int, str]:
        calls.append(list(argv))
        return 0, "Logged in as dev@example.com"

    cli_file = tmp_path / "cursor-agent"
    cli_file.write_text("")
    cli = CursorCli(cli_file, runner=runner, clock=lambda: now[0], ttl=10)
    assert (await cli.status())[0]
    assert (await cli.status())[0]
    now[0] = 11
    assert (await cli.status())[0]
    assert len(calls) == 2
    missing = CursorCli(tmp_path / "absent", runner=runner)
    ok, detail = await missing.status()
    assert not ok and "not installed" in detail


async def test_failing_models_command_is_unavailable(tmp_path: Path) -> None:
    async def runner(argv: Sequence[str]) -> tuple[int, str]:
        return (0, "ok") if argv[-1] == "status" else (1, "Error: network down")

    cli_file = tmp_path / "cursor-agent"
    cli_file.write_text("")
    with pytest.raises(AdapterUnavailableError, match="network down"):
        await CursorCli(cli_file, runner=runner).models()


def test_argv_adds_isolation_and_skips_auto(registry: Registry, tmp_path: Path) -> None:
    adapter = CursorCliAdapter(registry, CursorCli(tmp_path / "agent"))
    assert adapter.argv("/w", "auto", True) == [
        str(tmp_path / "agent"),
        "--workspace",
        "/w",
        "--worktree",
        "acp",
    ]


# mapping


def test_map_update_covers_text_and_tools() -> None:
    chunk: Params = {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "hi"}}
    assert m.map_update(chunk) == [m.Delta("hi")]
    thought: Params = {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "hmm"}}
    assert m.map_update(thought) == []
    started: Params = {"sessionUpdate": "tool_call", "toolCallId": "t", "kind": "read", "status": "pending"}
    assert m.map_update(started) == [m.ToolStart("t", "read", "")]
    instant: Params = {
        "sessionUpdate": "tool_call",
        "toolCallId": "t",
        "title": "Read",
        "status": "completed",
    }
    assert m.map_update(instant) == [m.ToolStart("t", "Read", ""), m.ToolEnd("t", "Read", True, "")]
    failed: Params = {"sessionUpdate": "tool_call_update", "toolCallId": "t", "status": "failed"}
    assert m.map_update(failed) == [m.ToolEnd("t", "tool", False, "")]
    progress: Params = {"sessionUpdate": "tool_call_update", "toolCallId": "t", "status": "in_progress"}
    assert m.map_update(progress) == []
    assert m.map_update({"sessionUpdate": "tool_call"}) == []


def test_pick_option_prefers_kinds_then_documented_ids() -> None:
    offered: JsonValue = [
        {"optionId": "yes", "kind": "allow_once"},
        {"optionId": "never", "kind": "reject_always"},
    ]
    assert m.pick_option(offered, "allow") == "yes"
    assert m.pick_option(offered, "deny") == "never"
    assert m.pick_option([{"optionId": "allow-always"}], "allow_session") == "allow-always"
    assert m.pick_option([], "deny") == "reject-once"
    assert m.permission_result([], None) == {"outcome": {"outcome": "cancelled"}}


def test_question_answers_resolve_ids_labels_and_numbers() -> None:
    params: Params = {
        "questions": [
            {
                "id": "q",
                "prompt": "Pick",
                "allowMultiple": True,
                "options": [{"id": "x", "label": "Ex"}, {"id": "y", "label": "Why"}],
            }
        ]
    }
    question, options = m.describe_questions(params)
    assert question == "Pick (several allowed: comma-separated)"
    assert options == ("Ex", "Why")
    result = m.question_result(params, "why, 1, nonsense, y")
    assert result == {
        "outcome": {"outcome": "answered", "answers": [{"questionId": "q", "selectedOptionIds": ["y", "x"]}]}
    }


def test_plan_results() -> None:
    assert m.plan_result("allow_session") == {"outcome": {"outcome": "accepted"}}
    assert m.plan_result(None) == {"outcome": {"outcome": "cancelled"}}
    assert m.describe_plan({"plan": "x" * 2000}).endswith("…")


# transport

ECHO = r"""
import json, sys

def send(**body):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", **body}) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("method") == "boom":
        sys.stderr.write("fatal: secret-free failure\n"); sys.stderr.flush(); sys.exit(4)
    if msg.get("method") == "ask":
        sys.stdout.write("not json\n")
        send(id=900, method="client/unknown", params={})
        send(id=msg["id"], result=json.loads(sys.stdin.readline()))
    elif msg.get("method") == "bad":
        send(id=msg["id"], error={"code": -1, "message": "nope"})
    elif "id" in msg:
        send(id=msg["id"], result=msg.get("params"))
"""


async def _echo(tmp_path: Path) -> AcpProcess:
    script = tmp_path / "echo.py"
    script.write_text(ECHO)

    async def on_request(method: str, params: Params) -> JsonValue:
        raise MethodNotFoundError(method)

    async def on_notification(method: str, params: Params) -> None:
        return None

    return await AcpProcess.spawn([sys.executable, str(script)], tmp_path, on_request, on_notification)


async def test_requests_errors_and_unknown_client_methods(tmp_path: Path) -> None:
    process = await _echo(tmp_path)
    assert await process.request("echo", {"a": 1}, 5) == {"a": 1}
    with pytest.raises(AcpRpcError, match="nope"):
        await process.request("bad", {}, 5)
    reply = await process.request("ask", {}, 5)
    assert isinstance(reply, dict)
    assert reply["error"] == {"code": -32601, "message": "method not found: client/unknown"}
    await process.close()
    assert not process.alive


async def test_an_exiting_process_fails_pending_requests(tmp_path: Path) -> None:
    process = await _echo(tmp_path)
    with pytest.raises(AcpExitedError, match=r"exited \(4\): fatal: secret-free failure"):
        await process.request("boom", {}, 5)
    with pytest.raises(AcpExitedError):
        await process.request("echo", {}, 5)
    await process.close()


async def test_spawning_a_missing_binary_is_unavailable(tmp_path: Path) -> None:
    async def on_request(method: str, params: Params) -> JsonValue:
        raise MethodNotFoundError(method)

    async def on_notification(method: str, params: Params) -> None:
        return None

    with pytest.raises(AcpExitedError, match="could not start"):
        await AcpProcess.spawn([str(tmp_path / "missing")], tmp_path, on_request, on_notification)
