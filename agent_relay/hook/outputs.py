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


def continue_with(hook: HookInput, text: str, shown: str | None = None) -> HookStdout:
    """Keep a stopping agent going, with `text` as its next prompt. `shown` is also shown in the chat
    where the harness supports it (VS Code)."""
    if hook.format is PayloadFormat.CURSOR:
        return {"followup_message": text}
    if hook.format is PayloadFormat.SNAKE and hook.kind is HookKind.STOP:
        return _shown(
            {
                "decision": "block",
                "reason": text,
                "hookSpecificOutput": {"hookEventName": hook.event, "decision": "block", "reason": text},
            },
            shown,
        )
    if hook.format is PayloadFormat.OPENCODE:
        return {"prompt": text}
    return {"decision": "block", "reason": text}


def add_context(hook: HookInput, text: str, shown: str | None = None) -> HookStdout:
    """Inject `text` into the conversation (steering, wake-up); `shown` as in `continue_with`."""
    if hook.format is PayloadFormat.CURSOR:
        return {"additional_context": text}
    if hook.format is PayloadFormat.OPENCODE:
        return {"context": text}
    if hook.format is PayloadFormat.SNAKE:
        return _shown({"hookSpecificOutput": {"hookEventName": hook.event, "additionalContext": text}}, shown)
    return {"additionalContext": text}
