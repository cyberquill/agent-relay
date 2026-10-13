"""Event bus with one ordered queue per session, and supervised adapters.

An exception in one sink or one adapter is logged and contained: other sessions keep flowing
and other adapters keep running.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Protocol

from agent_relay.adapters.base import Adapter, StatusReporter
from agent_relay.log import get_logger
from agent_relay.models.events import ErrorEvent, Event, Harness

log = get_logger(__name__)


class EventSink(Protocol):
    async def handle(self, event: Event) -> None: ...


class Router:
    def __init__(
        self, idle_worker_s: float = 300.0, restart_backoff: tuple[float, float] = (1.0, 60.0)
    ) -> None:
        self._sinks: list[EventSink] = []
        self._queues: dict[str, asyncio.Queue[Event]] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._adapters: dict[Harness, Adapter] = {}
        self._adapter_tasks: dict[Harness, asyncio.Task[None]] = {}
        self._idle_worker_s = idle_worker_s
        self._backoff_min, self._backoff_max = restart_backoff
        self.adapter_failures: dict[Harness, int] = {}
        self._lifecycle: StatusReporter | None = None

    def subscribe(self, sink: EventSink) -> None:
        self._sinks.append(sink)

    async def publish(self, event: Event) -> None:
        queue = self._queues.get(event.session_key)
        if queue is None:
            queue = asyncio.Queue()
            self._queues[event.session_key] = queue
        worker = self._workers.get(event.session_key)
        if worker is None or worker.done():
            self._workers[event.session_key] = asyncio.create_task(
                self._drain(event.session_key, queue), name=f"session:{event.session_key}"
            )
        await queue.put(event)

    async def _drain(self, session_key: str, queue: asyncio.Queue[Event]) -> None:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), self._idle_worker_s)
            except TimeoutError:
                if queue.empty():
                    self._queues.pop(session_key, None)
                    self._workers.pop(session_key, None)
                    return
                continue
            for sink in list(self._sinks):
                try:
                    await sink.handle(event)
                except Exception:
                    log.exception(f"sink {type(sink).__name__} failed on {event.type} for {session_key}")
            queue.task_done()

    async def join(self) -> None:
        """Wait until every queued event has been delivered (used by tests and shutdown)."""
        for queue in list(self._queues.values()):
            await queue.join()

    # adapters

    def adapter(self, harness: Harness) -> Adapter | None:
        return self._adapters.get(harness)

    def adapters(self) -> dict[Harness, Adapter]:
        return dict(self._adapters)

    def bind_lifecycle(self, lifecycle: StatusReporter) -> None:
        """Route the status changes adapters report through the session lifecycle."""
        self._lifecycle = lifecycle
        for adapter in self._adapters.values():
            adapter.lifecycle = lifecycle

    def add_adapter(self, adapter: Adapter) -> None:
        if self._lifecycle is not None:
            adapter.lifecycle = self._lifecycle
        self._adapters[adapter.harness] = adapter
        self._adapter_tasks[adapter.harness] = asyncio.create_task(
            self._supervise(adapter), name=f"adapter:{adapter.harness.value}"
        )

    async def _supervise(self, adapter: Adapter) -> None:
        delay = self._backoff_min
        while True:
            try:
                await adapter.run(self)
                delay = self._backoff_min
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.adapter_failures[adapter.harness] = self.adapter_failures.get(adapter.harness, 0) + 1
                log.exception(f"adapter {adapter.harness.value} crashed; restarting in {delay:.0f}s")
                await self.publish(
                    ErrorEvent(
                        harness=adapter.harness,
                        session_key=f"adapter:{adapter.harness.value}",
                        message=f"{adapter.harness.value} adapter error: {type(exc).__name__}",
                    )
                )
            await asyncio.sleep(delay)
            delay = min(delay * 2, self._backoff_max)

    async def stop(self) -> None:
        tasks = [*self._adapter_tasks.values(), *self._workers.values()]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        for adapter in self._adapters.values():
            with contextlib.suppress(Exception):
                await adapter.aclose()
        self._adapter_tasks.clear()
        self._workers.clear()
