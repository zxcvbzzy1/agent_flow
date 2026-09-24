"""Database-owned run state; Redis only caches snapshots and controls execution."""
import asyncio
import logging
from redis.exceptions import RedisError


class RunStateService:
    def __init__(self, store, runtime):
        self.store, self.runtime = store, runtime

    async def get(self, run_id):
        record = await self.runtime.cache.get(run_id, lambda: self.store.find_one('runs', {'run_id': run_id}))
        if not record:
            return None
        # A cancellation request is a live control signal, not a business status.
        try:
            control = await asyncio.wait_for(self.runtime.get_state(record.get('kind', 'orchestration'), run_id), 0.5)
        except (RedisError, OSError, TimeoutError):
            control = None
        return {**record, 'cancel_requested': bool(control and control.get('cancel_requested'))}

    async def create(self, record):
        item = self.store.insert_one('runs', record)
        await self.runtime.cache.invalidate(record['run_id'])
        return item

    async def update(self, run_id, changes):
        item = self.store.update_one('runs', {'run_id': run_id}, changes)
        if item is None:
            raise KeyError(f'Run 不存在: {run_id}')
        await self.runtime.cache.invalidate(run_id)
        if 'status' in changes:
            # Guarded by run_id: an older attempt cannot overwrite a regenerated message.
            for message in self.store.find_many('im_messages', {'run_id': run_id, 'sender_type': 'user'}):
                self.store.update_one('im_messages', {'message_id': message['message_id'], 'run_id': run_id},
                                      {'status': changes['status']})
        return item

    async def delete(self, run_id):
        record = self.store.find_one('runs', {'run_id': run_id}) or {}
        kind = record.get('kind', 'orchestration')
        control = await self.runtime.get_state(kind, run_id)
        if control and control.get('status') in {'pending', 'running'} and control.get('owner_worker'):
            await self.runtime.request_cancel(kind, run_id)
            for _ in range(50):
                await asyncio.sleep(0.1)
                control = await self.runtime.get_state(kind, run_id)
                if not control or control.get('status') not in {'pending', 'running'}:
                    break
            else:
                raise ValueError('执行仍在停止中，请稍后重试删除')
        stats = {
            'events': self.store.delete_many('events', {'run_id': run_id}),
            'scope_events': self.store.delete_many('im_events', {'run_id': run_id}),
            'runs': self.store.delete_one('runs', {'run_id': run_id}),
        }
        await self.runtime.delete_runtime(run_id, kind=kind)
        scope_id = record.get('scope_id') or run_id
        await self.runtime.remove_projected(scope_id, lambda event: event.get('run_id') == run_id)
        return stats

    async def finish_control(self, run_id):
        """Release only after final messages/events, so deletion can wait for the owner."""
        record = self.store.find_one('runs', {'run_id': run_id})
        if not record or record.get('status') not in {'finished', 'failed', 'cancelled'}:
            return
        try:
            await asyncio.wait_for(self.runtime.put_state(record.get('kind', 'orchestration'), run_id,
                                   {'status': record['status'], 'cancel_requested': False}), 1)
        except (RedisError, OSError, TimeoutError):
            logging.getLogger(__name__).exception('Database finalized; control release failed: %s', run_id)
