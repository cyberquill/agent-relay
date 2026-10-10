"""Normalise hook payloads from every harness into one `HookInput`.

Formats handled:
- Copilot family, camelCase (`sessionStart`, `agentStop`, `permissionRequest`, ...): Copilot CLI and
  the VS Code Copilot harness running on the Agent Host.
- Copilot family, PascalCase with snake_case fields (`PreToolUse`, `Stop`, ...): the VS
  Code-compatible format, which both the Copilot runtime and the VS Code harness send.
- Cursor (`preToolUse`, `stop`, `afterAgentResponse`, ...), snake_case fields.
- OpenCode plugin events (`tool.execute.before`, `session.idle`, `plugin.register`, ...).
"""

from __future__ import annotations

import json
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, JsonValue

from agent_relay.models.events import Harness


class HookFamily(StrEnum):
    COPILOT = "copilot"
    CURSOR = "cursor"
    OPENCODE = "opencode"


class HookKind(StrEnum):
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    PROMPT_SUBMIT = "prompt_submit"
    PRE_TOOL = "pre_tool"
    PERMISSION = "permission"
    POST_TOOL = "post_tool"
    TOOL_FAILURE = "tool_failure"
    STOP = "stop"
    SUBAGENT_STOP = "subagent_stop"
    NOTIFICATION = "notification"
    ERROR = "error"
    AGENT_RESPONSE = "agent_response"
    AGENT_THOUGHT = "agent_thought"
    FILE_EDIT = "file_edit"
    SHELL_BEFORE = "shell_before"
    MCP_BEFORE = "mcp_before"
    SHELL_AFTER = "shell_after"
    MCP_AFTER = "mcp_after"
    PLUGIN_REGISTER = "plugin_register"
    SESSION_IDLE = "session_idle"


class PayloadFormat(StrEnum):
    CAMEL = "camel"
    SNAKE = "snake"
    CURSOR = "cursor"
    OPENCODE = "opencode"


class UnknownHookEventError(ValueError):
    pass


_COPILOT_EVENTS: dict[str, HookKind] = {
    "sessionStart": HookKind.SESSION_START,
    "SessionStart": HookKind.SESSION_START,
    "sessionEnd": HookKind.SESSION_END,
    "SessionEnd": HookKind.SESSION_END,
    "userPromptSubmitted": HookKind.PROMPT_SUBMIT,
    "UserPromptSubmit": HookKind.PROMPT_SUBMIT,
    "preToolUse": HookKind.PRE_TOOL,
    "PreToolUse": HookKind.PRE_TOOL,
    "permissionRequest": HookKind.PERMISSION,
    "PermissionRequest": HookKind.PERMISSION,
    "postToolUse": HookKind.POST_TOOL,
    "PostToolUse": HookKind.POST_TOOL,
    "postToolUseFailure": HookKind.TOOL_FAILURE,
    "PostToolUseFailure": HookKind.TOOL_FAILURE,
    "agentStop": HookKind.STOP,
    "Stop": HookKind.STOP,
    "subagentStop": HookKind.SUBAGENT_STOP,
    "SubagentStop": HookKind.SUBAGENT_STOP,
    "notification": HookKind.NOTIFICATION,
    "Notification": HookKind.NOTIFICATION,
    "errorOccurred": HookKind.ERROR,
    "ErrorOccurred": HookKind.ERROR,
}

_CURSOR_EVENTS: dict[str, HookKind] = {
    "sessionStart": HookKind.SESSION_START,
    "sessionEnd": HookKind.SESSION_END,
    "beforeSubmitPrompt": HookKind.PROMPT_SUBMIT,
    "preToolUse": HookKind.PRE_TOOL,
    "postToolUse": HookKind.POST_TOOL,
    "postToolUseFailure": HookKind.TOOL_FAILURE,
    "beforeShellExecution": HookKind.SHELL_BEFORE,
    "beforeMCPExecution": HookKind.MCP_BEFORE,
    "afterShellExecution": HookKind.SHELL_AFTER,
    "afterMCPExecution": HookKind.MCP_AFTER,
    "afterFileEdit": HookKind.FILE_EDIT,
    "afterAgentResponse": HookKind.AGENT_RESPONSE,
    "afterAgentThought": HookKind.AGENT_THOUGHT,
    "stop": HookKind.STOP,
    "subagentStop": HookKind.SUBAGENT_STOP,
}

_OPENCODE_EVENTS: dict[str, HookKind] = {
    "plugin.register": HookKind.PLUGIN_REGISTER,
    "tool.execute.before": HookKind.PRE_TOOL,
    "tool.execute.after": HookKind.POST_TOOL,
    "session.idle": HookKind.SESSION_IDLE,
    "session.created": HookKind.SESSION_START,
    "session.error": HookKind.ERROR,
}


class HookInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    family: HookFamily
    harness: Harness
    event: str
    kind: HookKind
    format: PayloadFormat
    session_id: str
    workspace: str | None = None
    tool_name: str | None = None
    tool_input: JsonValue = None
    tool_output: str | None = None
    tool_use_id: str | None = None
    text: str | None = None
    stop_hook_active: bool = False
    loop_count: int = 0
    transcript_path: str | None = None
    notification_type: str | None = None
    status: str | None = None
    error: str | None = None
    mcp_server: str | None = None
    server_url: str | None = None


def _first_str(payload: dict[str, JsonValue], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _maybe_json(value: JsonValue) -> JsonValue:
    if isinstance(value, str):
        try:
            parsed: JsonValue = json.loads(value)
        except json.JSONDecodeError:
            return value
        return parsed
    return value


def _as_text(value: JsonValue) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("text_result_for_llm", "textResultForLlm", "output", "text"):
            inner = value.get(key)
            if isinstance(inner, str):
                return inner
    return json.dumps(value)


def _error_text(payload: dict[str, JsonValue]) -> str | None:
    err = payload.get("error")
    if isinstance(err, dict):
        msg = err.get("message")
        return msg if isinstance(msg, str) else json.dumps(err)
    if isinstance(err, str):
        return err
    return _first_str(payload, "error_message", "errorMessage")


def _workspace(payload: dict[str, JsonValue]) -> str | None:
    roots = payload.get("workspace_roots")
    if isinstance(roots, list) and roots and isinstance(roots[0], str):
        return roots[0]
    return _first_str(payload, "cwd", "directory", "workspace")


def _copilot_harness(event: str, kind: HookKind, payload: dict[str, JsonValue]) -> Harness:
    """VS Code's own agent vs the Copilot runtime. The relay later refines COPILOT_CLI into
    VSCODE_COPILOT for sessions it knows through the Agent Host."""
    if event[:1].islower():
        return Harness.COPILOT_CLI
    if kind in (HookKind.PRE_TOOL, HookKind.POST_TOOL) and "tool_use_id" in payload:
        return Harness.VSCODE
    if kind == HookKind.STOP and "stop_reason" not in payload:
        return Harness.VSCODE
    return Harness.COPILOT_CLI


def normalize(family: str, event: str, payload: dict[str, JsonValue]) -> HookInput:
    fam = HookFamily(family)
    if fam is HookFamily.COPILOT:
        kind = _COPILOT_EVENTS.get(event)
        fmt = PayloadFormat.CAMEL if event[:1].islower() else PayloadFormat.SNAKE
        if kind is None:
            raise UnknownHookEventError(f"copilot event {event}")
        harness = _copilot_harness(event, kind, payload)
    elif fam is HookFamily.CURSOR:
        kind = _CURSOR_EVENTS.get(event)
        fmt = PayloadFormat.CURSOR
        if kind is None:
            raise UnknownHookEventError(f"cursor event {event}")
        harness = Harness.CURSOR
    else:
        kind = _OPENCODE_EVENTS.get(event)
        fmt = PayloadFormat.OPENCODE
        if kind is None:
            raise UnknownHookEventError(f"opencode event {event}")
        harness = Harness.OPENCODE

    session_id = (
        _first_str(
            payload, "session_id", "sessionId", "conversation_id", "sessionID", "parent_conversation_id"
        )
        or "unknown"
    )
    tool_name = _first_str(payload, "tool_name", "toolName", "tool")
    tool_input: JsonValue
    if kind in (HookKind.SHELL_BEFORE, HookKind.SHELL_AFTER):
        tool_name = tool_name or "Shell"
        tool_input = {"command": payload.get("command"), "cwd": payload.get("cwd")}
    else:
        raw_input = payload.get("tool_input", payload.get("toolArgs", payload.get("args")))
        if raw_input is None and isinstance(payload.get("toolInput"), dict):
            raw_input = payload.get("toolInput")
        tool_input = _maybe_json(raw_input)
    output_raw = next(
        (
            payload[k]
            for k in ("tool_response", "tool_result", "toolResult", "tool_output", "output", "result_json")
            if k in payload
        ),
        None,
    )
    text = _first_str(payload, "text", "prompt", "initial_prompt", "initialPrompt", "message", "summary")
    loop = payload.get("loop_count")
    return HookInput(
        family=fam,
        harness=harness,
        event=event,
        kind=kind,
        format=fmt,
        session_id=session_id,
        workspace=_workspace(payload),
        tool_name=tool_name,
        tool_input=tool_input,
        tool_output=_as_text(output_raw),
        tool_use_id=_first_str(payload, "tool_use_id", "toolUseId", "callID", "call_id"),
        text=text,
        stop_hook_active=bool(payload.get("stop_hook_active", False)),
        loop_count=loop if isinstance(loop, int) else 0,
        transcript_path=_first_str(payload, "transcript_path", "transcriptPath", "agent_transcript_path"),
        notification_type=_first_str(payload, "notification_type"),
        status=_first_str(payload, "status", "reason", "stopReason", "stop_reason"),
        error=_error_text(payload),
        mcp_server=_first_str(payload, "mcp_server_name"),
        server_url=_first_str(payload, "serverUrl", "server_url"),
    )


def describe_tool(hook: HookInput, limit: int = 600) -> str:
    """A one-line, human-readable summary of a tool call for approval cards."""
    name = hook.tool_name or hook.kind.value
    inp = hook.tool_input
    detail = ""
    if isinstance(inp, dict):
        for key in ("command", "file_path", "filePath", "path", "url", "pattern", "query"):
            value = inp.get(key)
            if isinstance(value, str) and value:
                detail = value
                break
        if not detail and inp:
            detail = json.dumps(inp, ensure_ascii=False)
    elif isinstance(inp, str):
        detail = inp
    if hook.mcp_server:
        name = f"{hook.mcp_server}/{name}"
    summary = f"{name}: {detail}" if detail else name
    return summary if len(summary) <= limit else summary[: limit - 1] + "…"
