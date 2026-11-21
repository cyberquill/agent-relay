"""What a desk chat shows for a message the phone sent into it (VS Code `systemMessage`)."""

from __future__ import annotations

from agent_relay.core.parking import NOTICE_MAX, phone_notice, phone_text
from agent_relay.hook.outputs import ECHO_INSTRUCTION, add_context, continue_with
from agent_relay.hook.shims import normalize


def test_a_phone_continuation_is_shown_in_the_desk_chat_as_one_short_line() -> None:
    assert phone_notice(phone_text(["Reply with exactly: BACK"])) == "📱 Reply with exactly: BACK"
    assert phone_notice(phone_text(["one", "two\nthree"])) == "📱 one two three"
    long = phone_notice(phone_text(["x" * 900]))
    assert long is not None and len(long) == len("📱 ") + NOTICE_MAX and long.endswith("…")
    assert phone_notice("Parking renewed: do nothing") is None


def test_vs_code_gets_the_system_message_and_cursor_does_not() -> None:
    stop = normalize("copilot", "Stop", {"session_id": "H1", "cwd": "/w"})
    out = continue_with(stop, "text", "📱 hi")
    assert out["systemMessage"] == "📱 hi" and out["decision"] == "block"
    assert "systemMessage" not in continue_with(stop, "text")
    cursor = normalize("cursor", "stop", {"conversation_id": "K1", "workspace_roots": ["/w"]})
    assert continue_with(cursor, "text", "📱 hi") == {"followup_message": "text"}
    tool = normalize("copilot", "PostToolUse", {"session_id": "H1", "cwd": "/w", "tool_name": "x"})
    assert add_context(tool, "text", "📱 hi")["systemMessage"] == "📱 hi"


def test_the_agent_host_and_copilot_cli_get_the_line_as_a_request_to_say_it() -> None:
    stop = normalize("copilot", "agentStop", {"sessionId": "H1", "cwd": "/w"})
    out = continue_with(stop, "text", "📱 hi")
    assert out["decision"] == "block" and out["reason"] == f"text\n\n{ECHO_INSTRUCTION}\n📱 hi"
    assert continue_with(stop, "text")["reason"] == "text"
    assert add_context(stop, "text", "📱 hi")["additionalContext"] == f"text\n\n{ECHO_INSTRUCTION}\n📱 hi"
    snake = normalize("copilot", "Stop", {"session_id": "H1", "cwd": "/w"})
    assert continue_with(snake, "text", "📱 hi")["reason"] == "text"
