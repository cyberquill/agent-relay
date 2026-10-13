"""The card that closes a turn: what it cost and changed, and what to do next."""

from __future__ import annotations

from pydantic import JsonValue

from agent_relay.frontends.webex.cards.common import card, submit, text

TOP_FILES = 5
PATH_CHARS = 90
REPLY_CHARS = 4_000
REPLY_FIELD = "reply"


def duration_text(seconds: float) -> str:
    whole = max(0, round(seconds))
    if whole < 60:
        return f"{whole}s"
    minutes, secs = divmod(whole, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s" if secs else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if minutes else f"{hours}h"


def stats_line(seconds: float | None, tool_calls: int, files: int, added: int, removed: int) -> str:
    parts = []
    if seconds is not None:
        parts.append(f"⏱ {duration_text(seconds)}")
    parts.append(f"🔧 {tool_calls} tool call{'' if tool_calls == 1 else 's'}")
    if files:
        parts.append(f"📝 {files} file{'' if files == 1 else 's'} (+{added} −{removed})")
    return " · ".join(parts)


def _short(path: str) -> str:
    return path if len(path) <= PATH_CHARS else f"…{path[-(PATH_CHARS - 1) :]}"


def completion(
    session_key: str,
    stats: str,
    top_files: list[tuple[str, str]],
    more_files: int,
    *,
    nonces: dict[str, str],
    can_back: bool,
    quick_reply: bool,
) -> dict[str, JsonValue]:
    """`top_files` is (path, counts) pairs; `nonces` maps each button (`diff`, `continue`, `back`, `send`)
    to its own one-use nonce so tapping one leaves the others working."""

    def data(op: str) -> dict[str, JsonValue]:
        return {"relay": "completion", "op": op, "session_key": session_key, "nonce": nonces[op]}

    body: list[JsonValue] = [text("✅ Turn complete", weight="Bolder"), text(stats, subtle=True)]
    if top_files:
        body.append(text("\n".join(f"{_short(p)}  {c}" for p, c in top_files), mono=True, wrap=True))
    if more_files > 0:
        body.append(text(f"and {more_files} more", subtle=True))
    if quick_reply:
        body.append(
            {
                "type": "Input.Text",
                "id": REPLY_FIELD,
                "isMultiline": True,
                "placeholder": "Reply",
                "maxLength": REPLY_CHARS,
            }
        )
    actions: list[JsonValue] = [
        submit("Diff", data("diff")),
        submit("Continue", data("continue"), "positive"),
    ]
    if can_back:
        actions.append(submit("Back", data("back")))
    if quick_reply:
        actions.append(submit("Send", data("send"), "positive"))
    return card(body, actions)
