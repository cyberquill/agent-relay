"""Keyed wake-ups inside relayd. The registry holds the state; a signal only says "look again"."""

from __future__ import annotations

import asyncio
import contextlib


class Signals:
    def __init__(self) -> None:
        self._events: dict[str, set[asyncio.Event]] = {}

    def notify(self, key: str) -> None:
        for event in self._events.get(key, set()):
            event.set()

    def notify_all(self) -> None:
        for events in self._events.values():
            for event in events:
                event.set()

    async def wait(self, key: str, timeout: float) -> bool:
        """Wait until `key` is notified or `timeout` passes. Returns True when notified."""
        event = asyncio.Event()
        self._events.setdefault(key, set()).add(event)
        try:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(event.wait(), timeout)
            return event.is_set()
        finally:
            waiters = self._events.get(key)
            if waiters is not None:
                waiters.discard(event)
                if not waiters:
                    self._events.pop(key, None)

    def waiting(self, key: str) -> int:
        return len(self._events.get(key, set()))
