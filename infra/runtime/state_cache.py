"""Disposable run snapshots. Database writes always precede invalidation."""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
import uuid

from redis.exceptions import RedisError

log = logging.getLogger(__name__)


class RunStateCache:
    # Tokens outlive the maximum fill window, including after a run is deleted.
    _FILL = """
    if (redis.call('GET', KEYS[2]) or '') == ARGV[1] then
        return redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
    end
    return nil
    """
    _INVALIDATE = """
    redis.call('SET', KEYS[2], ARGV[1], 'EX', ARGV[2])
    return redis.call('DEL', KEYS[1])
    """

    def __init__(self, redis, key, ttl=5):
        self.redis, self.key, self.ttl = redis, key, ttl

    def keys(self, run_id):
        return (self.key(f'cache:v2:run:{run_id}'), self.key(f'cache:v2:generation:{run_id}'))

    async def get(self, run_id, loader):
        cache_key, generation_key = self.keys(run_id)
        started = time.monotonic()
        available = True
        try:
            raw, generation = await asyncio.wait_for(self.redis.mget(cache_key, generation_key), 0.5)
            if raw is not None:
                return json.loads(raw)
        except (RedisError, OSError, ValueError, TimeoutError):
            available = False
        value = loader()
        if inspect.isawaitable(value):
            value = await value
        if available and value is not None and time.monotonic() - started < self.ttl:
            try:
                await asyncio.wait_for(self.redis.eval(self._FILL, 2, cache_key, generation_key,
                                       generation or '', json.dumps(value, default=str), self.ttl), 0.5)
            except (RedisError, OSError, TimeoutError):
                log.warning('Run cache fill failed: %s', run_id)
        return value

    async def invalidate(self, run_id):
        for attempt in range(3):
            try:
                await asyncio.wait_for(self.redis.eval(self._INVALIDATE, 2, *self.keys(run_id),
                                       uuid.uuid4().hex, max(60, self.ttl * 2)), 0.5)
                return True
            except (RedisError, OSError, TimeoutError):
                if attempt < 2:
                    await asyncio.sleep((0.05, 0.15)[attempt])
        log.error('Run cache invalidation failed; TTL bounds stale reads: %s', run_id)
        return False
