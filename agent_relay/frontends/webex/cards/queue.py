"""The `/queue` card: the session's queue mode, and what is waiting, each removable."""

from __future__ import annotations

from pydantic import JsonValue

from agent_relay.frontends.webex.cards.common import card, choice_set, submit, text

MODE_LABELS = {
    "queue": "Queue: wait, then send as the next turn",
    "steer": "Steer: nudge the running turn",
    "interrupt": "Interrupt: stop the turn, then send",
}
ITEM_CHARS = 100
MAX_ITEMS = 10


def _short(value: str) -> str:
    flat = " ".join(value.split())
    return flat if len(flat) <= ITEM_CHARS else f"{flat[: ITEM_CHARS - 1]}…"


def queue_card(
    session_key: str,
    mode: str,
    items: list[tuple[int, str]],
    *,
    nonces: dict[str, str],
) -> dict[str, JsonValue]:
    """`items` is (id, text) of what waits. `nonces` has `mode`, `clear` and `remove:<id>` entries."""

    def data(op: str, nonce: str, **extra: JsonValue) -> dict[str, JsonValue]:
        return {"relay": "queue", "op": op, "session_key": session_key, "nonce": nonce, **extra}

    body: list[JsonValue] = [
        text("Queue mode", weight="Bolder"),
        choice_set("mode", [(m, label) for m, label in MODE_LABELS.items()], "compact", mode),
    ]
    if not items:
        body.append(text("Nothing is waiting.", subtle=True))
    for item_id, message in items[:MAX_ITEMS]:
        body.append(
            {
                "type": "ColumnSet",
                "columns": [
                    {"type": "Column", "width": "stretch", "items": [text(_short(message))]},
                    {
                        "type": "Column",
                        "width": "auto",
                        "items": [
                            {
                                "type": "ActionSet",
                                "actions": [
                                    submit(
                                        "Remove",
                                        data("remove", nonces[f"remove:{item_id}"], id=item_id),
                                        "destructive",
                                    )
                                ],
                            }
                        ],
                    },
                ],
            }
        )
    if len(items) > MAX_ITEMS:
        body.append(text(f"and {len(items) - MAX_ITEMS} more", subtle=True))
    actions: list[JsonValue] = [submit("Set mode", data("mode", nonces["mode"]), "positive")]
    if items:
        actions.append(submit("Clear all", data("clear", nonces["clear"]), "destructive"))
    return card(body, actions)
