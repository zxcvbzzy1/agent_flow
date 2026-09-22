"""Shared runtime transport. Tasks stay local; events and controls do not."""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import time
import uuid
from contextlib import suppress

from redis.asyncio import Redis
from redis.exceptions import WatchError

log = logging.getLogger(__name__)
ACTIVE = {"pending", "running"}


def encode(value):
    return json.dumps(value, ensure_ascii=False, default=str)


class RedisRuntime:
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

    async def _heartbeat(self):
        await self.redis.set(self.key(f"worker:{self.worker_id}"), "alive", ex=30)

    def track(self, kind, target_id, task):
        identity = f"{kind}:{target_id}"
        self._tasks[identity] = task

        def done(finished):
            if self._tasks.get(identity) is finished:
                self._tasks.pop(identity, None)
            if not finished.cancelled() and finished.exception():
                exc = finished.exception()
                log.error("Runtime task %s failed", identity, exc_info=(type(exc), exc, exc.__traceback__))

        task.add_done_callback(done)

    async def _maintain(self):
        ticks = 0
        while True:
            try:
                if ticks % 20 == 0:
                    await self._heartbeat()
                    await self.reap_orphans()
                for identity, task in list(self._tasks.items()):
                    state = await self.get_state(*identity.split(":", 1))
                    if state and ticks % 20 == 1:
                        scope = state.get("runtime_scope_id") or state.get("run_id")
                        if scope:
                            await self.redis.expire(self.key(f"route:{scope}"), self.retention)
                    if state and state.get("cancel_requested") and not task.done() and not task.cancelling():
                        task.cancel()
                ticks += 1
            except Exception:
                log.exception("Redis runtime control check failed")
            await asyncio.sleep(0.5)

    async def bind_scope(self, runtime_scope_id, scope_id):
        await self.redis.set(self.key(f"route:{runtime_scope_id}"), scope_id, ex=self.retention)

    async def append(self, category, scope_id, event):
        keys = [self.key(f"events:{category}:{scope_id}")]
        route_key = self.key(f"route:{scope_id}")
        route = await self.redis.get(route_key) if category == "run" else None
        if route:
            keys.append(self.key(f"events:scope:{route}"))
        cutoff = f"{max(0, int((time.time() - self.retention) * 1000))}-0"
        async with self.redis.pipeline(transaction=True) as pipe:
            for key in keys:
                pipe.xadd(key, {"event": encode(event)}, maxlen=self.maxlen, approximate=True)
                pipe.xtrim(key, minid=cutoff, approximate=True)
                pipe.expire(key, self.retention)
            if route:
                pipe.expire(route_key, self.retention)
            result = await pipe.execute()
        return result[0]

    @staticmethod
    def sse(event, cursor=None):
        prefix = f"id: {cursor}\n" if cursor is not None else ""
        return f"{prefix}event: {event['name']}\ndata: {encode(event)}\n\n"

    async def stream(self, category, scope_id, history, *, last_id=None, transform=None):
        """One cursor per delivered stream; no consumer group and no replay side effects."""
        key = self.key(f"events:{category}:{scope_id}")
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
        if not valid:
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
                yield self.sse(transform(event) if transform else event)
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
                    yield self.sse(event, cursor)

    async def get_state(self, kind, target_id):
        fields = await self.redis.hgetall(self.key(f"state:{kind}:{target_id}"))
        return {k: json.loads(v) for k, v in fields.items()} if fields else None

    async def put_state(self, kind, target_id, changes):
        key = self.key(f"state:{kind}:{target_id}")
        changes = {**changes, "kind": kind, "target_id": target_id, "updated_at": time.time()}
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.hset(key, mapping={k: encode(v) for k, v in changes.items()})
            status = changes.get("status")
            if status in ACTIVE:
                pipe.persist(key)
                pipe.sadd(self.key("active"), f"{kind}:{target_id}")
            elif status:
                pipe.expire(key, self.retention)
                pipe.srem(self.key("active"), f"{kind}:{target_id}")
            await pipe.execute()
        return await self.get_state(kind, target_id)

    async def claim(self, kind, target_id, fields):
        """Atomically start one attempt, allowing an explicit retry of terminal work."""
        await self._heartbeat()
        key = self.key(f"state:{kind}:{target_id}")
        while True:
            async with self.redis.pipeline() as pipe:
                try:
                    await pipe.watch(key)
                    raw = await pipe.hget(key, "status")
                    owner = await pipe.hget(key, "owner_worker")
                    if raw and json.loads(raw) in ACTIVE and owner and json.loads(owner):
                        return False
                    record = {**fields, "kind": kind, "target_id": target_id,
                              "owner_worker": self.worker_id, "cancel_requested": False,
                              "status": "running", "started_at": time.time(), "updated_at": time.time()}
                    pipe.multi()
                    pipe.delete(key)
                    pipe.hset(key, mapping={k: encode(v) for k, v in record.items()})
                    pipe.sadd(self.key("active"), f"{kind}:{target_id}")
                    await pipe.execute()
                    return True
                except WatchError:
                    continue

    async def active_states(self):
        result = []
        for identity in await self.redis.smembers(self.key("active")):
            state = await self.get_state(*identity.split(":", 1))
            if state and state.get("status") in ACTIVE:
                result.append(state)
        return result

    async def request_cancel(self, kind, target_id):
        key = self.key(f"state:{kind}:{target_id}")
        while True:
            async with self.redis.pipeline() as pipe:
                try:
                    await pipe.watch(key)
                    raw = await pipe.hget(key, "status")
                    if raw is None:
                        raise KeyError(f"运行不存在: {target_id}")
                    if json.loads(raw) not in ACTIVE:
                        return await self.get_state(kind, target_id)
                    pipe.multi()
                    pipe.hset(key, "cancel_requested", "true")
                    await pipe.execute()
                    return await self.get_state(kind, target_id)
                except WatchError:
                    continue

    async def reap_orphans(self):
        if self.on_orphan is None:
            return
        for state in await self.active_states():
            owner = state.get("owner_worker")
            if owner and not await self.redis.exists(self.key(f"worker:{owner}")):
                # Only one worker performs the business projection for this lost owner.
                lock = self.key(f"orphan:{state['kind']}:{state['target_id']}")
                if await self.redis.set(lock, "1", nx=True, ex=30):
                    await self.on_orphan(state)

    async def save_confirmation(self, item):
        key = self.key(f"confirmation:{item['confirmation_id']}")
        await self.redis.set(key, encode(item))
        await self.redis.sadd(self.key(f"confirmations:{item['run_id']}"), item['confirmation_id'])

    async def get_confirmation(self, confirmation_id):
        raw = await self.redis.get(self.key(f"confirmation:{confirmation_id}"))
        return json.loads(raw) if raw else None

    async def confirmations(self, run_id):
        result = []
        for cid in await self.redis.smembers(self.key(f"confirmations:{run_id}")):
            item = await self.get_confirmation(cid)
            if item and item["status"] == "pending":
                result.append(item)
        return result

    async def resolve_confirmation(self, run_id, confirmation_id, approved, reason):
        key = self.key(f"confirmation:{confirmation_id}")
        while True:
            async with self.redis.pipeline() as pipe:
                try:
                    await pipe.watch(key)
                    raw = await pipe.get(key)
                    item = json.loads(raw) if raw else None
                    if not item or item["run_id"] != run_id:
                        raise KeyError(f"confirmation not found: {confirmation_id}")
                    if item["status"] != "pending":
                        return item, False
                    item.update(status="resolved", approved=approved, reason=reason,
                                resolved_at=time.time())
                    pipe.multi()
                    pipe.set(key, encode(item), ex=self.retention)
                    pipe.srem(self.key(f"confirmations:{run_id}"), confirmation_id)
                    await pipe.execute()
                    return item, True
                except WatchError:
                    continue

    async def delete_runtime(self, runtime_scope_id, *, kind="orchestration", target_id=None):
        route = await self.redis.get(self.key(f"route:{runtime_scope_id}"))
        if route:
            await self.remove_projected(route, lambda e: e.get("run_id") == runtime_scope_id)
        for item in await self.confirmations(runtime_scope_id):
            await self.redis.delete(self.key(f"confirmation:{item['confirmation_id']}"))
        target_id = target_id or runtime_scope_id
        await self.redis.delete(self.key(f"events:run:{runtime_scope_id}"),
                                self.key(f"route:{runtime_scope_id}"),
                                self.key(f"state:{kind}:{target_id}"),
                                self.key(f"confirmations:{runtime_scope_id}"))
        await self.redis.srem(self.key("active"), f"{kind}:{target_id}")

    async def remove_projected(self, scope_id, predicate):
        key = self.key(f"events:scope:{scope_id}")
        cursor = "-"
        while True:
            batch = await self.redis.xrange(key, min=cursor, count=500)
            if not batch:
                return
            ids = [rid for rid, fields in batch if predicate(json.loads(fields['event']))]
            if ids:
                await self.redis.xdel(key, *ids)
            cursor = "(" + batch[-1][0]
