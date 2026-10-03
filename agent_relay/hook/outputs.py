"""Render relay decisions in each harness's own hook output format."""

from __future__ import annotations

from pydantic import JsonValue

from agent_relay.hook.shims import HookInput, HookKind, PayloadFormat

type HookStdout = dict[str, JsonValue]


def permission(hook: HookInput, allow: bool, reason: str) -> HookStdout:
    decision = "allow" if allow else "deny"
    if hook.format is PayloadFormat.CURSOR:
        out: HookStdout = {"permission": decision}
        if not allow:
            out["user_message"] = reason
            out["agent_message"] = reason
        return out
    if hook.format is PayloadFormat.OPENCODE:
        return {"decision": decision, "reason": reason}
    if hook.kind is HookKind.PERMISSION:
        result: HookStdout = {"behavior": decision}
        if not allow:
            result["message"] = reason
        return result
    if hook.format is PayloadFormat.SNAKE:
        return {
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
            "hookSpecificOutput": {
                "hookEventName": hook.event,
                "permissionDecision": decision,
                "permissionDecisionReason": reason,
            },
        }
    return {"permissionDecision": decision, "permissionDecisionReason": reason}


def continue_with(hook: HookInput, text: str) -> HookStdout:
    """Keep a stopping agent going, with `text` as its next prompt."""
    if hook.format is PayloadFormat.CURSOR:
        return {"followup_message": text}
    if hook.format is PayloadFormat.SNAKE and hook.kind is HookKind.STOP:
        return {
            "decision": "block",
            "reason": text,
            "hookSpecificOutput": {"hookEventName": hook.event, "decision": "block", "reason": text},
        }
    if hook.format is PayloadFormat.OPENCODE:
        return {"prompt": text}
    return {"decision": "block", "reason": text}


def add_context(hook: HookInput, text: str) -> HookStdout:
    """Inject `text` into the conversation (steering, wake-up)."""
    if hook.format is PayloadFormat.CURSOR:
        return {"additional_context": text}
    if hook.format is PayloadFormat.OPENCODE:
        return {"context": text}
    if hook.format is PayloadFormat.SNAKE:
        return {"hookSpecificOutput": {"hookEventName": hook.event, "additionalContext": text}}
    return {"additionalContext": text}
