from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

from infra.db.mongodb import DocumentStore


class EventStreamService:
    def __init__(self, store: DocumentStore) -> None:
        self._store = store
        self._queues: dict[str, list[asyncio.Queue[dict[str, Any]]]] = {}

    def publish(self, run_id: str, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        event = {
            "event_id": str(uuid.uuid4()),
            "run_id": run_id,
            "name": name,
            "payload": payload,
            "created_at": time.time(),
        }
        self._store.insert_one("events", event)
        for queue in list(self._queues.get(run_id, [])):
            queue.put_nowait(event)
        return event

    def no_store_publish(self, run_id: str, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        event = {
            "event_id": str(uuid.uuid4()),
            "run_id": run_id,
            "name": name,
            "payload": payload,
            "created_at": time.time(),
        }
        for queue in list(self._queues.get(run_id, [])):
            queue.put_nowait(event)
        return event

    def list_events(self, run_id: str) -> list[dict[str, Any]]:
        return self._store.find_many(
            "events",
            {"run_id": run_id},
            sort=[("created_at", 1)],
        )

    async def stream(self, run_id: str):
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._queues.setdefault(run_id, []).append(queue)
        try:
            for event in self.list_events(run_id):
                yield self.format_sse(event)
                if event["name"] in {"workflow.finished", "workflow.failed"}:
                    return
            while True:
                event = await queue.get()
                yield self.format_sse(event)
                if event["name"] in {"workflow.finished", "workflow.failed"}:
                    break
        finally:
            self._queues.get(run_id, []).remove(queue)

    def format_sse(self, event: dict[str, Any]) -> str:
        payload = json.dumps(event, ensure_ascii=False, default=str)
        return f"event: {event['name']}\ndata: {payload}\n\n"
