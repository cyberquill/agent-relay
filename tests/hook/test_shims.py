"""AC-29: every documented hook payload fixture normalises to the expected HookInput."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import JsonValue

from agent_relay.hook import outputs
from agent_relay.hook.shims import HookKind, UnknownHookEventError, abbreviate, describe_tool, normalize

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "hooks"


def _cases() -> list[tuple[str, str, dict[str, JsonValue], dict[str, JsonValue]]]:
    cases: list[tuple[str, str, dict[str, JsonValue], dict[str, JsonValue]]] = []
    for file in sorted(FIXTURES.glob("*.json")):
        for case in json.loads(file.read_text(encoding="utf-8")):
            cases.append((file.stem, case["event"], case["payload"], case["expect"]))
    return cases


CASES = _cases()


def test_fixture_set_covers_every_family() -> None:
    assert {family for family, *_ in CASES} == {"copilot", "cursor", "opencode"}
    assert len(CASES) >= 25


@pytest.mark.parametrize(("family", "event", "payload", "expect"), CASES)
def test_fixture_normalises(
    family: str, event: str, payload: dict[str, JsonValue], expect: dict[str, JsonValue]
) -> None:
    hook = normalize(family, event, payload)
    dumped = hook.model_dump(mode="json")
    for key, value in expect.items():
        assert dumped[key] == value, f"{family}/{event}: {key}"


def test_unknown_event_raises() -> None:
    with pytest.raises(UnknownHookEventError):
        normalize("cursor", "somethingNew", {})


def test_missing_session_id_is_tolerated() -> None:
    assert normalize("copilot", "agentStop", {}).session_id == "unknown"


def test_describe_tool_prefers_command_and_keeps_the_destructive_tail() -> None:
    hook = normalize(
        "cursor", "beforeShellExecution", {"conversation_id": "c", "command": "x" * 900 + "; rm -rf ~"}
    )
    text = describe_tool(hook, limit=100)
    assert text.startswith("Shell: xxx") and text.endswith("; rm -rf ~")
    assert "more chars" in text and len(text) <= 100


def test_abbreviate_marks_what_it_cut_and_leaves_short_text_alone() -> None:
    assert abbreviate("short", 600) == "short"
    text = "H" * 500 + "M" * 1000 + "T" * 500
    cut = abbreviate(text, 200)
    omitted = int(cut.split(" … ")[1].split()[0])
    assert cut.startswith("HHH") and cut.endswith("TTT") and len(cut) <= 200
    assert omitted == len(text) - (len(cut) - len(f" … {omitted} more chars … "))
    assert abbreviate("y" * 50, 10) == "y … 48 more chars … y"  # a tiny limit still shows both ends


def test_describe_tool_names_mcp_server() -> None:
    hook = normalize(
        "cursor", "beforeMCPExecution", {"conversation_id": "c", "tool_name": "t", "mcp_server_name": "gh"}
    )
    assert describe_tool(hook) == "gh/t"


def test_permission_outputs_per_format() -> None:
    vscode = normalize("copilot", "PreToolUse", {"session_id": "s", "tool_use_id": "t"})
    assert outputs.permission(vscode, False, "no")["hookSpecificOutput"] == {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "no",
    }
    camel = normalize("copilot", "preToolUse", {"sessionId": "s"})
    assert outputs.permission(camel, True, "ok") == {
        "permissionDecision": "allow",
        "permissionDecisionReason": "ok",
    }
    perm = normalize("copilot", "permissionRequest", {"sessionId": "s"})
    assert outputs.permission(perm, False, "no") == {"behavior": "deny", "message": "no"}
    assert outputs.permission(perm, True, "") == {"behavior": "allow"}
    cursor = normalize("cursor", "beforeShellExecution", {"conversation_id": "c", "command": "ls"})
    assert outputs.permission(cursor, False, "no") == {
        "permission": "deny",
        "user_message": "no",
        "agent_message": "no",
    }
    oc = normalize("opencode", "tool.execute.before", {"sessionID": "s"})
    assert outputs.permission(oc, False, "no") == {"decision": "deny", "reason": "no"}


def test_continue_and_context_outputs() -> None:
    stop = normalize("copilot", "Stop", {"session_id": "s"})
    assert stop.kind is HookKind.STOP
    cont = outputs.continue_with(stop, "next")
    assert cont["decision"] == "block" and cont["reason"] == "next"
    assert outputs.continue_with(normalize("copilot", "agentStop", {}), "go") == {
        "decision": "block",
        "reason": "go",
    }
    assert outputs.continue_with(normalize("cursor", "stop", {}), "go") == {"followup_message": "go"}
    post = normalize("copilot", "PostToolUse", {"session_id": "s", "tool_use_id": "t"})
    assert outputs.add_context(post, "steer") == {
        "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "steer"}
    }
    assert outputs.add_context(normalize("cursor", "postToolUse", {}), "s") == {"additional_context": "s"}
    assert outputs.add_context(normalize("copilot", "postToolUse", {}), "s") == {"additionalContext": "s"}
