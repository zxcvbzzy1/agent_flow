from __future__ import annotations

import inspect
import time
import uuid

from infra.runtime import RedisRuntime


class EventStreamService:
    """Mongo event history with a Redis broadcast/replay stream."""

    def __init__(self, store, runtime: RedisRuntime | None = None):
        self._store = store
        self.runtime = runtime or RedisRuntime()
        self._subscribers = []

    def subscribe(self, callback):
        self._subscribers.append(callback)

    async def publish(self, run_id, name, payload):
        event = self._event(run_id, name, payload)
        self._store.insert_one("events", event)
        await self.runtime.append("run", run_id, event)
        for callback in self._subscribers:
            result = callback(event)
            if inspect.isawaitable(result):
                await result
        return event

    async def no_store_publish(self, run_id, name, payload):
        """Retained in Redis for reconnects, but not archived in Mongo."""
        event = self._event(run_id, name, payload)
        await self.runtime.append("run", run_id, event)
        return event

    @staticmethod
    def _event(run_id, name, payload):
        return dict(event_id=str(uuid.uuid4()), run_id=run_id, name=name,
                    payload=payload, created_at=time.time())

    def list_events(self, run_id):
        return self._store.find_many("events", {"run_id": run_id}, sort=[("created_at", 1)])

    async def stream(self, run_id, last_id=None):
        async for raw in self.runtime.stream("run", run_id, lambda: self.list_events(run_id), last_id=last_id):
            yield raw

    def format_sse(self, event, cursor=None):
        return self.runtime.sse(event, cursor)
