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
    echoed = f"text\n\n{ECHO_INSTRUCTION}\n📱 hi"
    stop = normalize("copilot", "agentStop", {"sessionId": "H1", "cwd": "/w"})
    out = continue_with(stop, "text", "📱 hi", echo=True)
    assert out["decision"] == "block" and out["reason"] == echoed
    assert continue_with(stop, "text", echo=True)["reason"] == "text"
    assert add_context(stop, "text", "📱 hi", echo=True)["additionalContext"] == echoed


def test_the_echo_follows_the_app_not_the_hook_casing() -> None:
    """The installed `Stop` and `PostToolUse` are PascalCase, so the Agent Host's arrive snake case (D141)."""
    echoed = f"text\n\n{ECHO_INSTRUCTION}\n📱 hi"
    snake = normalize("copilot", "Stop", {"session_id": "H1", "cwd": "/w", "stop_reason": "end_turn"})
    out = continue_with(snake, "text", "📱 hi", echo=True)
    assert out["reason"] == echoed and out["hookSpecificOutput"]["reason"] == echoed  # type: ignore[index,call-overload]
    assert continue_with(snake, "text", "📱 hi")["reason"] == "text"
    tool = normalize("copilot", "PostToolUse", {"session_id": "H1", "cwd": "/w", "tool_name": "x"})
    context = add_context(tool, "text", "📱 hi", echo=True)["hookSpecificOutput"]
    assert context["additionalContext"] == echoed  # type: ignore[index,call-overload]
    camel = normalize("copilot", "agentStop", {"sessionId": "H1", "cwd": "/w"})
    assert continue_with(camel, "text", "📱 hi")["reason"] == "text"
