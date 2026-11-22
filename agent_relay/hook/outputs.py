"""Render relay decisions in each harness's own hook output format."""

from __future__ import annotations

from pydantic import JsonValue

from agent_relay.hook.shims import HookInput, HookKind, PayloadFormat

type HookStdout = dict[str, JsonValue]


def allow(hook: HookInput, reason: str) -> HookStdout:
    """The harness's own "allow this tool call" answer. The relay never denies a tool call."""
    if hook.format is PayloadFormat.CURSOR:
        return {"permission": "allow"}
    if hook.format is PayloadFormat.OPENCODE:
        return {"decision": "allow", "reason": reason}
    if hook.kind is HookKind.PERMISSION:
        return {"behavior": "allow"}
    if hook.format is PayloadFormat.SNAKE:
        return {
            "permissionDecision": "allow",
            "permissionDecisionReason": reason,
            "hookSpecificOutput": {
                "hookEventName": hook.event,
                "permissionDecision": "allow",
                "permissionDecisionReason": reason,
            },
        }
    return {"permissionDecision": "allow", "permissionDecisionReason": reason}


def _shown(out: HookStdout, shown: str | None) -> HookStdout:
    """VS Code's `systemMessage`: a line in the chat itself, so the desk sees what the phone sent."""
    return {**out, "systemMessage": shown} if shown else out


ECHO_INSTRUCTION = "Start your reply with this line, exactly as written:"


def _echoed(text: str, shown: str | None, echo: bool) -> str:
    """The Copilot runtime (the Copilot CLI and the VS Code Agent Host) reads only the hook's `reason` or
    `additionalContext` and shows nothing else, in either hook casing, so the agent is asked to say the
    line itself: that is what the desk then sees."""
    if not shown or not echo:
        return text
    return f"{text}\n\n{ECHO_INSTRUCTION}\n{shown}"


def continue_with(hook: HookInput, text: str, shown: str | None = None, *, echo: bool = False) -> HookStdout:
    """Keep a stopping agent going, with `text` as its next prompt. `shown` is also shown in the chat
    where the harness supports it (VS Code), and with `echo` the agent is asked to say it."""
    if hook.format is PayloadFormat.CURSOR:
        return {"followup_message": text}
    if hook.format is PayloadFormat.OPENCODE:
        return {"prompt": text}
    reason = _echoed(text, shown, echo)
    if hook.format is PayloadFormat.SNAKE and hook.kind is HookKind.STOP:
        return _shown(
            {
                "decision": "block",
                "reason": reason,
                "hookSpecificOutput": {"hookEventName": hook.event, "decision": "block", "reason": reason},
            },
            shown,
        )
    return _shown({"decision": "block", "reason": reason}, shown)


def add_context(hook: HookInput, text: str, shown: str | None = None, *, echo: bool = False) -> HookStdout:
    """Inject `text` into the conversation (steering, wake-up); `shown` and `echo` as in `continue_with`."""
    if hook.format is PayloadFormat.CURSOR:
        return {"additional_context": text}
    if hook.format is PayloadFormat.OPENCODE:
        return {"context": text}
    context = _echoed(text, shown, echo)
    if hook.format is PayloadFormat.SNAKE:
        return _shown(
            {"hookSpecificOutput": {"hookEventName": hook.event, "additionalContext": context}}, shown
        )
    return _shown({"additionalContext": context}, shown)
