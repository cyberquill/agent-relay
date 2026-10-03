"""A fake `cursor-agent`: `status`, `models` and `acp` (newline-delimited JSON-RPC on stdio).

Behaviour is chosen by the prompt text, and by flags in `$FAKE_ACP_DIR/config.json`. Every message
it receives is appended to `$FAKE_ACP_DIR/acp.log`, and each run's argv to `argv.log`.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

DIR = Path(os.environ.get("FAKE_ACP_DIR", "/tmp"))
MODELS_TEXT = "Available models\n\nauto - Auto (current)\ngpt-5 - GPT-5\nsonnet-4 - Claude 4 Sonnet\n"


def config() -> dict[str, Any]:
    file = DIR / "config.json"
    return json.loads(file.read_text()) if file.exists() else {}


def log(name: str, value: object) -> None:
    with (DIR / name).open("a") as handle:
        handle.write(json.dumps(value) + "\n")


class Agent:
    def __init__(self) -> None:
        self.cfg = config()
        self.backlog: list[dict[str, Any]] = []
        self.cancelled = False
        self.sessions = 0
        self.next_id = 100

    def send(self, message: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
        sys.stdout.flush()

    def read(self) -> dict[str, Any] | None:
        line = sys.stdin.readline()
        if not line:
            return None
        message: dict[str, Any] = json.loads(line)
        log("acp.log", message)
        if message.get("method") == "session/cancel":
            self.cancelled = True
        return message

    def wait_reply(self, call_id: int) -> dict[str, Any]:
        while True:
            message = self.read()
            if message is None:
                sys.exit(0)
            if message.get("id") == call_id and "method" not in message:
                return message
            if message.get("method") != "session/cancel":
                self.backlog.append(message)

    def ask(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.next_id += 1
        self.send({"id": self.next_id, "method": method, "params": params})
        return self.wait_reply(self.next_id)

    def update(self, sid: str, update: dict[str, Any]) -> None:
        self.send({"method": "session/update", "params": {"sessionId": sid, "update": update}})

    def chunk(self, sid: str, text: str) -> None:
        self.update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}})

    def serve(self) -> None:
        while True:
            message = self.backlog.pop(0) if self.backlog else self.read()
            if message is None:
                return
            method, call_id = message.get("method"), message.get("id")
            if method is None or call_id is None:
                continue
            params = message.get("params") or {}
            try:
                result = self.handle(method, params)
            except RpcError as exc:
                self.send({"id": call_id, "error": {"code": exc.code, "message": exc.message}})
                continue
            self.send({"id": call_id, "result": result})

    def handle(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            return {
                "protocolVersion": 1,
                "agentCapabilities": {"loadSession": not self.cfg.get("no_load")},
                "authMethods": [{"id": "cursor_login", "name": "Cursor login"}],
            }
        if method == "authenticate":
            if self.cfg.get("auth_fail"):
                raise RpcError(-32000, "Authentication required")
            return {}
        if method == "session/new":
            self.sessions += 1
            result: dict[str, Any] = {"sessionId": f"acp-{os.getpid()}-{self.sessions}"}
            if not self.cfg.get("no_models"):
                result["models"] = {
                    "availableModels": [{"modelId": "gpt-5", "name": "GPT-5"}],
                    "currentModelId": "auto",
                }
            return result
        if method == "session/load":
            self.chunk(params["sessionId"], "OLD HISTORY")
            return {}
        if method == "session/set_model":
            if self.cfg.get("set_model_fail"):
                raise RpcError(-32601, "method not found")
            return {}
        if method == "session/prompt":
            return self.prompt(params["sessionId"], params["prompt"][0]["text"])
        raise RpcError(-32601, f"unknown method {method}")

    def prompt(self, sid: str, text: str) -> dict[str, Any]:
        self.cancelled = False
        if "crash" in text:
            sys.exit(3)
        if "fail" in text:
            raise RpcError(-32000, "model overloaded")
        if "use a tool" in text:
            return self.tool(sid)
        if "ask twice" in text:
            return self.questions(sid, two=True)
        if "ask me" in text:
            return self.questions(sid, two=False)
        if "plan first" in text:
            reply = self.ask(
                "cursor/create_plan",
                {"toolCallId": "pl1", "name": "Refactor", "plan": "1. a\n2. b", "todos": []},
            )
            self.chunk(sid, f"plan {reply['result']['outcome']['outcome']}")
            return {"stopReason": "end_turn"}
        if "wait" in text:
            self.chunk(sid, "working")
            while not self.cancelled:
                message = self.read()
                if message is None:
                    sys.exit(0)
                if message.get("method") != "session/cancel":
                    self.backlog.append(message)
            return {"stopReason": "cancelled"}
        if "limit" in text:
            self.chunk(sid, "partial")
            return {"stopReason": "max_tokens"}
        self.chunk(sid, "O")
        self.chunk(sid, "K")
        return {"stopReason": "end_turn"}

    def tool(self, sid: str) -> dict[str, Any]:
        call = {
            "toolCallId": "tc1",
            "title": "Run ls",
            "kind": "execute",
            "status": "pending",
            "rawInput": {"command": "ls"},
        }
        self.update(sid, {"sessionUpdate": "tool_call", **call})
        options = [
            {"optionId": "allow-once", "name": "Allow", "kind": "allow_once"},
            {"optionId": "allow-always", "name": "Always", "kind": "allow_always"},
            {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"},
        ]
        reply = self.ask(
            "session/request_permission", {"sessionId": sid, "toolCall": call, "options": options}
        )
        outcome = reply["result"]["outcome"]
        allowed = outcome.get("optionId", "").startswith("allow")
        status = "completed" if allowed else "failed"
        content = [{"type": "content", "content": {"type": "text", "text": "file.txt"}}]
        self.update(
            sid,
            {"sessionUpdate": "tool_call_update", "toolCallId": "tc1", "status": status, "content": content},
        )
        self.chunk(sid, f"tool {outcome.get('optionId', outcome['outcome'])}")
        return {"stopReason": "cancelled" if self.cancelled else "end_turn"}

    def questions(self, sid: str, two: bool) -> dict[str, Any]:
        first = {
            "id": "q1",
            "prompt": "Which mode?",
            "options": [{"id": "agent", "label": "Agent"}, {"id": "plan", "label": "Plan"}],
        }
        second = {
            "id": "q2",
            "prompt": "Which files?",
            "allowMultiple": True,
            "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
        }
        reply = self.ask(
            "cursor/ask_question",
            {"toolCallId": "aq1", "title": "Need input", "questions": [first, second] if two else [first]},
        )
        self.chunk(sid, json.dumps(reply["result"]["outcome"], sort_keys=True))
        return {"stopReason": "end_turn"}


class RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def main(argv: list[str]) -> int:
    log("argv.log", argv)
    cfg = config()
    if "status" in argv:
        if cfg.get("logged_out"):
            sys.stdout.write("Not logged in\n")
            return 1
        sys.stdout.write("Logged in as dev@example.com\n")
        return 0
    if "models" in argv:
        sys.stdout.write(MODELS_TEXT)
        return 0
    if "acp" in argv:
        Agent().serve()
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
