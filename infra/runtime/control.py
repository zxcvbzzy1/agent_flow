"""Cross-worker execution leases, cancellation and approval controls."""
from __future__ import annotations
import asyncio
import json
import logging
import time
from redis.exceptions import WatchError

log = logging.getLogger(__name__)
ACTIVE = {"pending", "running"}


def encode(value):
    return json.dumps(value, ensure_ascii=False, default=str)


class RedisExecutionControl:
    CONTROL_FIELDS = frozenset({
        "run_id", "message_id", "agent_id", "conversation_id", "scope_id",
        "owner_worker", "status", "cancel_requested", "started_at", "finished_at",
    })

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
                    if state and state.get("cancel_requested") and not task.done() and not task.cancelling():
                        task.cancel()
                ticks += 1
            except Exception:
                log.exception("Redis runtime control check failed")
            await asyncio.sleep(0.5)

    async def get_state(self, kind, target_id):
        fields = await self.redis.hgetall(self.key(f"control:v2:{kind}:{target_id}"))
        return {k: json.loads(v) for k, v in fields.items()} if fields else None

    async def put_state(self, kind, target_id, changes):
        key = self.key(f"control:v2:{kind}:{target_id}")
        # This hash is a lease/control record, never a business-state cache.
        changes = {k: v for k, v in changes.items() if k in self.CONTROL_FIELDS}
        changes = {**changes, "kind": kind, "target_id": target_id, "updated_at": time.time()}
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.hset(key, mapping={k: encode(v) for k, v in changes.items()})
            status = changes.get("status")
            if status in ACTIVE:
                pipe.persist(key)
                pipe.sadd(self.key("control:v2:active"), f"{kind}:{target_id}")
            elif status:
                pipe.expire(key, self.retention)
                pipe.srem(self.key("control:v2:active"), f"{kind}:{target_id}")
            await pipe.execute()
        return await self.get_state(kind, target_id)

    async def claim(self, kind, target_id, fields):
        """Atomically start one attempt, allowing an explicit retry of terminal work."""
        fields = {k: v for k, v in fields.items() if k in self.CONTROL_FIELDS}
        await self._heartbeat()
        key = self.key(f"control:v2:{kind}:{target_id}")
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
                    pipe.sadd(self.key("control:v2:active"), f"{kind}:{target_id}")
                    await pipe.execute()
                    return True
                except WatchError:
                    continue

    async def active_states(self):
        result = []
        for identity in await self.redis.smembers(self.key("control:v2:active")):
            state = await self.get_state(*identity.split(":", 1))
            if state and state.get("status") in ACTIVE:
                result.append(state)
        return result

    async def request_cancel(self, kind, target_id):
        key = self.key(f"control:v2:{kind}:{target_id}")
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

