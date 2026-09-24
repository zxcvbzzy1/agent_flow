"""Shared runtime transport. Tasks stay local; events and controls do not."""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import time
import uuid
from contextlib import suppress

from redis.asyncio import Redis
from .control import RedisExecutionControl
from .state_cache import RunStateCache


def encode(value):
    return json.dumps(value, ensure_ascii=False, default=str)


class RedisRuntime(RedisExecutionControl):
    def __init__(self, *, url=None, prefix=None, retention=86400, maxlen=100000):
        self.url = url or os.getenv("REDIS_URL", "redis://localhost:6379/0")
        self.prefix = prefix or os.getenv("REDIS_KEY_PREFIX", "agenthub")
        self.retention = retention
        self.maxlen = maxlen
        # XREAD blocks for up to 15 seconds while an SSE connection is idle.
        # redis-py 8 defaults to a 5-second read timeout, so explicitly allow
        # the blocking interval plus network/server scheduling headroom.
        self.redis = Redis.from_url(
            self.url, decode_responses=True, socket_connect_timeout=5, socket_timeout=30,
        )
        self.worker_id = str(uuid.uuid4())
        self._tasks = {}
        self._maintenance = None
        self.on_orphan = None
        self.cache = RunStateCache(self.redis, self.key)

    def key(self, suffix):
        return f"{self.prefix}:{suffix}"

    async def start(self):
        if self._maintenance is not None:
            return
        try:
            await self.redis.ping()
            await self._heartbeat()
        except Exception:
            await self.redis.aclose()
            raise
        self._maintenance = asyncio.create_task(self._maintain(), name="redis-runtime-controls")

    async def close(self):
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._maintenance:
            self._maintenance.cancel()
            with suppress(asyncio.CancelledError):
                await self._maintenance
            self._maintenance = None
        try:
            await self.redis.delete(self.key(f"worker:{self.worker_id}"))
        finally:
            await self.redis.aclose()

    async def append(self, category, scope_id, event):
        key = self.event_key(category, scope_id)
        cutoff = f"{max(0, int((time.time() - self.retention) * 1000))}-0"
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.xadd(key, {"event": encode(event)}, maxlen=self.maxlen, approximate=True)
            pipe.xtrim(key, minid=cutoff, approximate=True)
            pipe.expire(key, self.retention)
            result = await pipe.execute()
        return result[0]

    def event_key(self, category, scope_id):
        return self.key(f"events:v2:{category}:{scope_id}")

    @staticmethod
    def sse(event, cursor=None):
        prefix = f"id: {cursor}\n" if cursor is not None else ""
        return f"{prefix}event: {event['name']}\ndata: {encode(event)}\n\n"

    async def stream(self, category, scope_id, history, *, last_id=None, transform=None, reconcile=False):
        """One cursor per delivered stream; no consumer group and no replay side effects."""
        key = self.event_key(category, scope_id)
        await self.redis.xtrim(key, minid=f"{max(0, int((time.time() - self.retention) * 1000))}-0",
                               approximate=False)
        first = await self.redis.xrange(key, count=1)
        last = await self.redis.xrevrange(key, count=1)
        high = last[0][0] if last else "0-0"
        valid = False
        if last_id:
            try:
                cursor_tuple = tuple(map(int, last_id.split("-")))
                valid = len(cursor_tuple) == 2 and bool(first) and (
                    tuple(map(int, first[0][0].split("-"))) <= cursor_tuple <= tuple(map(int, high.split("-")))
                )
            except ValueError:
                pass
        seen = set()
        if last_id and not valid:
            yield self.sse({"name": "stream.reset", "payload": {"reason": "cursor_expired"}})
        if not valid or reconcile:
            archived = history()
            if inspect.isawaitable(archived):
                archived = await archived
            buffered = []
            cursor = "-"
            while high != "0-0":
                batch = await self.redis.xrange(key, min=cursor, max=high, count=500)
                if not batch:
                    break
                buffered.extend(json.loads(fields["event"]) for _, fields in batch)
                cursor = "(" + batch[-1][0]
            for event in sorted([*archived, *buffered], key=lambda e: e.get("created_at", 0)):
                if event["event_id"] in seen:
                    continue
                seen.add(event["event_id"])
                delivered = transform(event) if transform else event
                if delivered is not None:
                    yield self.sse(delivered)
            last_id = high
        yield self.sse({"name": "stream.ready", "payload": {"scope_id": scope_id}}, last_id)
        # Retain bootstrap IDs until the first empty read: an archive write can precede XADD.
        while True:
            batches = await self.redis.xread({key: last_id}, count=200, block=15000)
            if not batches:
                seen.clear()
                yield ": heartbeat\n\n"
            for _, entries in batches:
                for cursor, fields in entries:
                    event = json.loads(fields["event"])
                    last_id = cursor
                    if event["event_id"] in seen:
                        yield f"id: {cursor}\n\n"
                        continue
                    delivered = transform(event) if transform else event
                    yield self.sse(delivered, cursor) if delivered is not None else f"id: {cursor}\n\n"

    async def delete_runtime(self, runtime_scope_id, *, kind="orchestration", target_id=None):
        for item in await self.confirmations(runtime_scope_id):
            await self.redis.delete(self.key(f"confirmation:{item['confirmation_id']}"))
        await self.cache.invalidate(runtime_scope_id)
        target_id = target_id or runtime_scope_id
        await self.redis.delete(self.event_key("run", runtime_scope_id),
                                self.key(f"control:v2:{kind}:{target_id}"),
                                self.key(f"confirmations:{runtime_scope_id}"))
        await self.redis.srem(self.key("control:v2:active"), f"{kind}:{target_id}")

    async def remove_projected(self, scope_id, predicate):
        key = self.event_key("scope", scope_id)
        cursor = "-"
        while True:
            batch = await self.redis.xrange(key, min=cursor, count=500)
            if not batch:
                return
            ids = [rid for rid, fields in batch if predicate(json.loads(fields['event']))]
            if ids:
                await self.redis.xdel(key, *ids)
            cursor = "(" + batch[-1][0]
