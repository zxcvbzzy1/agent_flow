"""Shared runtime transport. Tasks stay local; events and controls do not."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import suppress

from redis.asyncio import Redis
from .control import RedisExecutionControl
from .state_cache import RunStateCache
from .event_journal import EventJournal, encode, ordered


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
        self.journal = EventJournal(self)

    def key(self, suffix):
        return f"{self.prefix}:{suffix}"

    async def start(self):
        if self._maintenance is not None:
            return
        try:
            await self.redis.ping()
            version = (await self.redis.info("server"))["redis_version"]
            if tuple(int(v) for v in version.split(".")[:2]) < (6, 2):
                raise RuntimeError("Event archiving requires Redis >= 6.2")
            self.journal.aof_enabled = bool((await self.redis.info('persistence')).get('aof_enabled'))
            if self.journal.store is not None and not self.journal.aof_enabled:
                logging.getLogger(__name__).warning(
                    'Redis AOF is disabled: enable appendonly yes before deploying asynchronous event archiving')
            await self._heartbeat()
            await self.journal.start()
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
        await self.journal.close()
        try:
            await self.redis.delete(self.key(f"worker:{self.worker_id}"))
        finally:
            await self.redis.aclose()

    async def append(self, category, scope_id, event, *, durable=False):
        if durable and self._maintenance is not None:
            await self.journal.start()
        return await self.journal.append(category, scope_id, event, durable=durable)

    def event_key(self, category, scope_id):
        return self.journal.keys(category, scope_id)[0]

    @staticmethod
    def sse(event, cursor=None):
        prefix = f"id: {cursor}\n" if cursor is not None else ""
        return f"{prefix}event: {event['name']}\ndata: {encode(event)}\n\n"

    async def stream(self, category, scope_id, history, *, last_id=None, transform=None,
                     reconcile=False, user_id='service'):
        """Opaque generation/cursor IDs; each reader gets every event independently.

        ``reconcile`` is retained for caller compatibility; complete cached history
        replaces the former unconditional DB reconciliation.
        """
        keys = self.journal.keys(category, scope_id)
        key, meta = keys[:2]
        await self.redis.hsetnx(meta, 'generation', uuid.uuid4().hex)
        generation = await self.redis.hget(meta, 'generation')
        seen = set()
        while True:
            await self.redis.xtrim(key, minid=f"{max(0, int((time.time() - self.retention) * 1000))}-0",
                                   approximate=False)
            first = await self.redis.xrange(key, count=1)
            # ``high`` is the legacy name. Take the newer value while old and
            # new workers overlap during a rolling restart.
            watermark = self.journal.latest_stream_id(
                *(await self.redis.hmget(meta, 'watermark', 'high')))
            cursor = '0-0'
            valid = False
            if last_id:
                try:
                    prior_generation, cursor = last_id.split('/', 1)
                    position = tuple(map(int, cursor.split('-')))
                    watermark_position = tuple(map(int, watermark.split('-')))
                    valid = (prior_generation == generation and len(position) == 2 and
                             all(v >= 0 for v in position) and position <= watermark_position and
                             ((cursor == watermark == '0-0') or (bool(first) and
                              tuple(map(int, first[0][0].split('-'))) <= position)))
                except (ValueError, AttributeError):
                    valid = False
            if not valid:
                yield self.sse({'name': 'stream.reset', 'payload': {
                    'reason': 'cursor_expired' if last_id else 'initial'}}, '')
                archived, cursor, generation = await self.journal.snapshot(
                    category, scope_id, history, user_id=user_id)
                # The durable snapshot is never trimmed. Only transient events
                # need the bounded live stream during bootstrap.
                buffered = []
                start = '-'
                while cursor != '0-0':
                    batch = await self.redis.xrange(key, min=start, max=cursor, count=500)
                    if not batch:
                        break
                    buffered.extend(json.loads(fields['event']) for _, fields in batch)
                    start = '(' + batch[-1][0]
                deleted = await self.redis.smembers(keys[4])
                for event in ordered([*archived, *buffered]):
                    if event['event_id'] in seen or '*' in deleted or event['event_id'] in deleted:
                        continue
                    seen.add(event['event_id'])
                    delivered = transform(event) if transform else event
                    if delivered is not None:
                        yield self.sse(delivered)
            await self.journal.touch(category, scope_id, user_id)
            yield self.sse({'name': 'stream.ready', 'payload': {'scope_id': scope_id}},
                           f'{generation}/{cursor}')
            while True:
                batches = await self.redis.xread({key: cursor}, count=200, block=15000)
                current_generation = await self.redis.hget(meta, 'generation')
                # A deletion invalidates both persisted browser snapshots and
                # connections already open, even if there are no new events.
                if current_generation != generation:
                    last_id = f'{generation}/{cursor}'
                    generation = current_generation
                    seen.clear()
                    break
                if batches:
                    # Detect a slow reader falling behind live retention/maxlen.
                    first = await self.redis.xrange(key, count=1)
                    if cursor != '0-0' and first and tuple(map(int, cursor.split('-'))) < tuple(map(int, first[0][0].split('-'))):
                        last_id = f'{generation}/{cursor}'
                        seen.clear()
                        break
                if not batches:
                    seen.clear()
                    await self.journal.touch(category, scope_id, user_id)
                    yield ': heartbeat\n\n'
                for _, entries in batches:
                    for cursor, fields in entries:
                        event = json.loads(fields['event'])
                        checkpoint = f'{generation}/{cursor}'
                        if event['event_id'] in seen:
                            yield self.sse({'name': 'stream.checkpoint', 'payload': {}}, checkpoint)
                            continue
                        delivered = transform(event) if transform else event
                        yield self.sse(delivered, checkpoint) if delivered is not None else self.sse({'name': 'stream.checkpoint', 'payload': {}}, checkpoint)

    async def delete_runtime(self, runtime_scope_id, *, kind="orchestration", target_id=None):
        for item in await self.confirmations(runtime_scope_id):
            await self.redis.delete(self.key(f"confirmation:{item['confirmation_id']}"))
        await self.cache.invalidate(runtime_scope_id)
        await self.journal.delete("run", runtime_scope_id)
        target_id = target_id or runtime_scope_id
        await self.redis.delete(self.event_key("run", runtime_scope_id),
                                self.key(f"control:v2:{kind}:{target_id}"),
                                self.key(f"confirmations:{runtime_scope_id}"))
        await self.redis.srem(self.key("control:v2:active"), f"{kind}:{target_id}")

    async def remove_projected(self, scope_id, predicate):
        return await self.journal.delete('scope', scope_id, predicate)
