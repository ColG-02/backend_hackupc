"""
Simple in-process SSE event bus.

Producers call `await bus.publish(event_type, payload)` from anywhere.
The SSE endpoint subscribes with `bus.subscribe()`, drains the queue,
and calls `bus.unsubscribe(q)` on disconnect.
"""

import asyncio
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


class EventBus:
    def __init__(self) -> None:
        self._queues: list[asyncio.Queue] = []

    async def publish(self, event_type: str, payload: dict) -> None:
        if not self._queues:
            return
        message = {"event": event_type, "data": payload, "ts": datetime.utcnow().isoformat() + "Z"}
        dead: list[asyncio.Queue] = []
        for q in self._queues:
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            logger.warning("SSE subscriber queue full — dropping slow client.")
            try:
                self._queues.remove(q)
            except ValueError:
                pass

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._queues.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        try:
            self._queues.remove(q)
        except ValueError:
            pass


bus = EventBus()
