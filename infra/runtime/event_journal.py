"""Complete user histories and a separate, non-expiring acknowledged archive queue.

All keys belong to one database namespace. SSE readers never consume the archive
consumer group. Only acknowledged queue entries may be removed.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager, suppress

from redis.exceptions import ResponseError

log = logging.getLogger(__name__)


def encode(value):
    return json.dumps(value, ensure_ascii=False, default=str)


def ordered(events):
    return sorted(events, key=lambda e: (e.get('created_at', 0), e['event_id']))


# KEYS: live, metadata, subscribers, pending bodies, tombstones, archive, dedup, scopes
_APPEND = """
if redis.call('SISMEMBER', KEYS[5], '*') == 1 or
   redis.call('SISMEMBER', KEYS[5], ARGV[1]) == 1 then return false end
local previous = redis.call('HGET', KEYS[7], ARGV[1])
if previous then return previous end
redis.call('HSETNX', KEYS[2], 'generation', ARGV[3])
redis.call('SADD', KEYS[8], ARGV[9])
local id = redis.call('XADD', KEYS[1], '*', 'event', ARGV[2])
redis.call('HSET', KEYS[2], 'watermark', id)
redis.call('HSET', KEYS[7], ARGV[1], id)
redis.call('EXPIRE', KEYS[7], ARGV[4])
redis.call('XTRIM', KEYS[1], 'MAXLEN', '~', ARGV[5])
redis.call('XTRIM', KEYS[1], 'MINID', '~', ARGV[6])
redis.call('EXPIRE', KEYS[1], ARGV[4])
if ARGV[7] ~= '' then
    redis.call('HSET', KEYS[4], ARGV[1], ARGV[2])
    redis.call('XADD', KEYS[6], '*', 'category', ARGV[8], 'scope', ARGV[9],
               'collection', ARGV[7], 'event', ARGV[2], 'operation', 'insert')
end
for _, cache in ipairs(redis.call('SMEMBERS', KEYS[3])) do
    if redis.call('EXISTS', cache) == 1 then
        if ARGV[7] ~= '' then redis.call('HSET', cache, ARGV[1], ARGV[2]) end
        redis.call('HSET', cache, '__watermark', id)
        redis.call('EXPIRE', cache, ARGV[10])
    else redis.call('SREM', KEYS[3], cache) end
end
redis.call('EXPIRE', KEYS[3], ARGV[10])
return id
"""

_BEGIN = """
redis.call('HSETNX', KEYS[2], 'generation', ARGV[1])
local function latest(a, b)
    local am, as = string.match(a or '0-0', '^(%d+)%-(%d+)$')
    local bm, bs = string.match(b or '0-0', '^(%d+)%-(%d+)$')
    am, as, bm, bs = tonumber(am), tonumber(as), tonumber(bm), tonumber(bs)
    if bm > am or (bm == am and bs > as) then return b end
    return a or '0-0'
end
local watermark = latest(redis.call('HGET', KEYS[2], 'watermark'),
                         redis.call('HGET', KEYS[2], 'high'))
redis.call('DEL', KEYS[1])
redis.call('HSET', KEYS[1], '__token', ARGV[2], '__ready', '0',
           '__generation', redis.call('HGET', KEYS[2], 'generation'),
           '__watermark', watermark)
local pending = redis.call('HGETALL', KEYS[4])
for i=1,#pending,2 do redis.call('HSET', KEYS[1], pending[i], pending[i+1]) end
redis.call('EXPIRE', KEYS[1], ARGV[3])
redis.call('SADD', KEYS[3], KEYS[1])
redis.call('EXPIRE', KEYS[3], ARGV[3])
return redis.call('HGET', KEYS[2], 'generation')
"""

_FILL = """
if redis.call('HGET', KEYS[1], '__token') ~= ARGV[1] then return 0 end
for i=2,#ARGV,2 do
    if redis.call('SISMEMBER', KEYS[2], '*') == 0 and
       redis.call('SISMEMBER', KEYS[2], ARGV[i]) == 0 then
        redis.call('HSETNX', KEYS[1], ARGV[i], ARGV[i+1])
    end
end
return 1
"""

_FINISH = """
if redis.call('HGET', KEYS[1], '__token') ~= ARGV[1] then return 0 end
redis.call('HSET', KEYS[1], '__ready', '1')
redis.call('EXPIRE', KEYS[1], ARGV[2])
return 1
"""

_READ = """
if redis.call('HGET', KEYS[1], '__ready') ~= '1' then return {} end
redis.call('EXPIRE', KEYS[1], ARGV[1])
redis.call('EXPIRE', KEYS[2], ARGV[1])
return redis.call('HGETALL', KEYS[1])
"""

_ACK = """
for i=2,#ARGV,2 do
    redis.call('XACK', KEYS[1], ARGV[1], ARGV[i])
    redis.call('XDEL', KEYS[1], ARGV[i])
    redis.call('HDEL', KEYS[2], ARGV[i+1])
end
return 1
"""

# Persist tombstones before DB deletion. A queued delete repairs a crash between
# marking Redis and deleting Mongo. Invalidate staging caches as well as ready ones.
_DELETE = """
redis.call('HSET', KEYS[2], 'generation', ARGV[1])
for _, cache in ipairs(redis.call('SMEMBERS', KEYS[3])) do redis.call('DEL', cache) end
redis.call('DEL', KEYS[3])
for i=5,#ARGV do
    redis.call('SADD', KEYS[5], ARGV[i])
    redis.call('HDEL', KEYS[4], ARGV[i])
end
if ARGV[5] == '*' then redis.call('DEL', KEYS[1], KEYS[4]) end
redis.call('XADD', KEYS[6], '*', 'operation', 'delete', 'category', ARGV[2],
           'scope', ARGV[3], 'collection', ARGV[4], 'ids', cjson.encode({unpack(ARGV,5)}))
return 1
"""


class EventJournal:
    group = 'archive-v1'

    def __init__(self, runtime, *, ttl=172800):
        self.runtime, self.redis, self.ttl = runtime, runtime.redis, ttl
        self.store = None
        self.namespace = 'service'
        self.aof_enabled = None
        self.task = None
        self._start_lock = asyncio.Lock()
        self.stop = asyncio.Event()
        self.wakeup = asyncio.Event()
        self.batch_size = 500
        self.claim_idle_ms = 30000
        self.claim_cursor = '0-0'

    def bind(self, store):
        namespace = getattr(store, 'event_namespace', 'service')
        if self.store is not None and namespace != self.namespace:
            raise ValueError('A runtime cannot archive events into multiple databases')
        if self.store is None and hasattr(store, 'ensure_index'):
            for collection in ('events', 'im_events'):
                store.ensure_index(collection, [('event_id', 1)], unique=True)
        self.store, self.namespace = store, namespace

    def key(self, suffix):
        return self.runtime.key(f'events:v3:{self.namespace}:{suffix}')

    def keys(self, category, scope):
        base = self.key(f'{category}:{scope}')
        return [base + suffix for suffix in (':live', ':meta', ':readers', ':pending', ':deleted')]

    @property
    def queue(self):
        return self.key('archive')

    def cache_key(self, category, scope, user_id):
        identity = hashlib.sha256(str(user_id).encode()).hexdigest()
        return self.key(f'{category}:{scope}:user:{identity}')

    async def start(self):
        async with self._start_lock:
            if self.task or self.store is None:
                return
            await self.ensure_group()
            self.stop.clear()
            self.task = asyncio.create_task(self._work(), name='event-archive')

    async def ensure_group(self):
        try:
            await self.redis.xgroup_create(self.queue, self.group, id='0-0', mkstream=True)
        except ResponseError as exc:
            if 'BUSYGROUP' not in str(exc):
                raise

    async def close(self):
        if self.task:
            self.stop.set()
            self.wakeup.set()
            # Never cancel an in-flight DB operation and then ACK it speculatively.
            await self.task
            self.task = None

    @asynccontextmanager
    async def lock(self, name):
        lock = self.redis.lock(self.key('lock:' + name), timeout=30, blocking_timeout=60)
        if not await lock.acquire():
            raise TimeoutError('Event journal lock unavailable')
        async def renew():
            while True:
                await asyncio.sleep(5)
                await lock.extend(30, replace_ttl=True)
        task = asyncio.create_task(renew())
        try:
            yield
            if task.done():
                task.result()
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            if await lock.owned():
                await lock.release()

    async def append(self, category, scope, event, *, durable=False):
        # Deletion must enumerate a stable set of durable events before marking
        # their IDs. Live token delivery doesn't participate in DB deletion.
        if durable:
            async with self.lock(f'flow:{category}:{scope}'):
                return await self._append(category, scope, event, durable=True)
        return await self._append(category, scope, event)

    async def _append(self, category, scope, event, *, durable=False):
        if durable and self.store is None:
            raise RuntimeError('Event archive store is not bound')
        keys = self.keys(category, scope)
        collection = ('im_events' if category == 'scope' else 'events') if durable else ''
        cursor = await self.redis.eval(_APPEND, 8, *keys, self.queue,
            self.key(f'{category}:{scope}:dedup'), self.key(f'{category}:scopes'), event['event_id'], encode(event), uuid.uuid4().hex,
            self.runtime.retention, self.runtime.maxlen,
            f'{max(0, int((time.time() - self.runtime.retention) * 1000))}-0',
            collection, category, scope, self.ttl)
        if cursor is None:
            raise ValueError('Cannot publish a deleted event or stream')
        if durable:
            # Remote workers poll every second; this wakeup just accelerates local batches.
            if await self.redis.xlen(self.queue) >= self.batch_size:
                self.wakeup.set()
        return cursor

    async def touch(self, category, scope, user_id):
        async with self.redis.pipeline(transaction=False) as pipe:
            pipe.expire(self.cache_key(category, scope, user_id), self.ttl)
            pipe.expire(self.keys(category, scope)[2], self.ttl)
            await pipe.execute()

    async def snapshot(self, category, scope, history, *, user_id='service'):
        cache = self.cache_key(category, scope, user_id)
        keys = self.keys(category, scope)
        async def read():
            raw = await self.redis.eval(_READ, 2, cache, keys[2], self.ttl)
            return dict(zip(raw[::2], raw[1::2])) if raw else None
        value = await read()
        if value:
            await self.redis.hincrby(self.key('metrics'), 'cache_hits', 1)
            return self._snapshot(value)
        async with self.lock('fill:' + cache):
            value = await read()
            if value:
                return self._snapshot(value)
            await self.redis.hincrby(self.key('metrics'), 'cache_misses', 1)
            # Register the staging hash before reading Mongo. Every subsequent
            # durable publish also updates it, including events ACKed during the read.
            for _ in range(3):
                token = uuid.uuid4().hex
                await self.redis.eval(_BEGIN, 4, cache, keys[1], keys[2], keys[3],
                                      uuid.uuid4().hex, token, self.ttl)
                await self.redis.hincrby(self.key('metrics'), 'history_loads', 1)
                archived = await self._load(history)
                valid = True
                for start in range(0, len(archived), 500):
                    args = [v for event in archived[start:start + 500]
                            for v in (event['event_id'], encode(event))]
                    if not await self.redis.eval(_FILL, 2, cache, keys[4], token, *args):
                        valid = False
                        break
                if valid and await self.redis.eval(_FINISH, 1, cache, token, self.ttl):
                    value = await read()
                    if value:
                        return self._snapshot(value)
            raise RuntimeError('Event history changed repeatedly during initialization')

    @staticmethod
    async def _load(loader):
        if inspect.iscoroutinefunction(loader):
            return await loader()
        value = await asyncio.to_thread(loader)
        return await value if inspect.isawaitable(value) else value

    @staticmethod
    def _snapshot(value):
        # ``__high`` is a read-only migration alias for caches created before
        # the field was renamed. Comparing both also supports rolling restarts.
        watermark = EventJournal.latest_stream_id(
            value.get('__watermark'), value.get('__high'))
        return (ordered(json.loads(raw) for key, raw in value.items() if not key.startswith('__')),
                watermark, value['__generation'])

    @staticmethod
    def latest_stream_id(*values):
        def position(value):
            try:
                return tuple(map(int, value.split('-', 1)))
            except (AttributeError, TypeError, ValueError):
                return (0, 0)
        return max((value for value in values if value), key=position, default='0-0')

    def history(self, category, scope):
        collection, query = self.query(category, scope)
        return self.store.find_many(collection, query, sort=[('created_at', 1), ('event_id', 1)])

    @staticmethod
    def query(category, scope):
        return ('im_events', {'scope_id': scope, 'version': 2}) if category == 'scope' else ('events', {'run_id': scope})

    async def events(self, category, scope, *, user_id='service'):
        events, _, _ = await self.snapshot(category, scope, lambda: self.history(category, scope), user_id=user_id)
        return events

    async def delete(self, category, scope, predicate=None):
        """Tombstones survive cache expiry and prevent delayed archive resurrection."""
        keys = self.keys(category, scope)
        async with self.lock('archive'), self.lock(f'flow:{category}:{scope}'):
            events = await self.events(category, scope) if self.store else []
            live = await self.redis.xrange(keys[0])
            events = {e['event_id']: e for e in [*events, *(json.loads(f['event']) for _, f in live)]}
            ids = [eid for eid, event in events.items() if predicate is None or predicate(event)]
            if not ids and predicate is not None:
                return 0
            collection, query = self.query(category, scope)
            targets = ['*'] if predicate is None else ids
            for offset in range(0, len(targets), 500):
                await self.redis.eval(_DELETE, 6, *keys, self.queue, uuid.uuid4().hex,
                                      category, scope, collection, *targets[offset:offset + 500])
            removed = [rid for rid, fields in live if json.loads(fields['event'])['event_id'] in ids]
            if removed:
                await self.redis.xdel(keys[0], *removed)
            if self.store:
                await asyncio.to_thread(self.store.delete_many, collection,
                                        query if predicate is None else {'event_id': {'$in': ids}})
            return len(ids)

    async def flush_once(self):
        """One recoverable batch. Exposed for lifecycle tests and maintenance drains."""
        async with self.lock('archive'):
            claimed = await self.redis.xautoclaim(self.queue, self.group, self.runtime.worker_id,
                min_idle_time=self.claim_idle_ms, start_id=self.claim_cursor, count=self.batch_size)
            self.claim_cursor = claimed[0]
            rows = list(claimed[1])
            if len(rows) < self.batch_size:
                fresh = await self.redis.xreadgroup(self.group, self.runtime.worker_id,
                    {self.queue: '>'}, count=self.batch_size - len(rows))
                rows.extend(row for _, entries in fresh for row in entries)
            if not rows:
                return 0
            # Group by flow so the pending-body cleanup and deletion guard use
            # the right namespace. Failed/unknown outcomes remain in the PEL.
            grouped = {}
            for rid, fields in rows:
                grouped.setdefault((fields['category'], fields['scope']), []).append((rid, fields))
            confirmed = 0
            for (category, scope), batch in grouped.items():
                keys = self.keys(category, scope)
                deleted = await self.redis.smembers(keys[4])
                inserts, acknowledgements = [], []
                for rid, fields in batch:
                    if fields['operation'] == 'delete':
                        try:
                            ids = json.loads(fields['ids'])
                            collection, query = self.query(category, scope)
                            await asyncio.to_thread(self.store.delete_many, collection,
                                query if ids == ['*'] else {'event_id': {'$in': ids}})
                            acknowledgements.append((rid, ''))
                        except Exception:
                            log.exception('Event archive deletion failed: %s', rid)
                    else:
                        event = json.loads(fields['event'])
                        if '*' in deleted or event['event_id'] in deleted:
                            acknowledgements.append((rid, event['event_id']))
                        else:
                            inserts.append((rid, event))
                if inserts:
                    try:
                        collection, _ = self.query(category, scope)
                        successes = await asyncio.to_thread(self.store.upsert_events, collection,
                                                           [e for _, e in inserts])
                        # Recheck after I/O in case a worker lost its Redis lease
                        # while a DB operation was in flight. Tombstones win.
                        latest_deleted = await self.redis.smembers(keys[4])
                        removed = [e['event_id'] for _, e in inserts
                                   if '*' in latest_deleted or e['event_id'] in latest_deleted]
                        if removed:
                            await asyncio.to_thread(self.store.delete_many, collection, {'event_id': {'$in': removed}})
                        acknowledgements.extend((rid, e['event_id']) for rid, e in inserts
                                                if e['event_id'] in successes)
                        failures = len(inserts) - len(successes)
                        if failures:
                            await self.redis.hincrby(self.key('metrics'), 'archive_failures', failures)
                            log.error('Event archive left %s unconfirmed entries for retry', failures)
                    except Exception:
                        await self.redis.hincrby(self.key('metrics'), 'archive_failures', 1)
                        log.exception('Event archive batch failed; pending entries retained')
                if acknowledgements:
                    await self.redis.eval(_ACK, 2, self.queue, keys[3], self.group,
                                          *(x for pair in acknowledgements for x in pair))
                    confirmed += len(acknowledgements)
            await self.redis.hincrby(self.key('metrics'), 'archived', confirmed)
            return confirmed

    async def stats(self):
        oldest = await self.redis.xrange(self.queue, count=1)
        return {'aof_enabled': self.aof_enabled, 'queued': await self.redis.xlen(self.queue),
                'oldest_age_seconds': max(0, time.time() - int(oldest[0][0].split('-')[0]) / 1000) if oldest else 0,
                'pending': (await self.redis.xpending(self.queue, self.group))['pending'],
                **{k: int(v) for k, v in (await self.redis.hgetall(self.key('metrics'))).items()}}

    async def _work(self):
        last_report = 0
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(self.wakeup.wait(), 1)
            except TimeoutError:
                pass
            self.wakeup.clear()
            try:
                count = await self.flush_once()
                if count >= self.batch_size:
                    self.wakeup.set()
                if time.monotonic() - last_report >= 60:
                    last_report = time.monotonic()
                    stats = await self.stats()
                    if stats['oldest_age_seconds'] > 60:
                        log.warning('Event archive backlog: %s', stats)
            except ResponseError as exc:
                if 'NOGROUP' in str(exc):
                    try:
                        await self.ensure_group()
                    except Exception:
                        log.exception('Event archive group recovery failed; will retry')
                else:
                    log.exception('Event archive worker failed; will retry')
            except Exception:
                log.exception('Event archive worker failed; will retry')
