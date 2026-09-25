from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from contextlib import asynccontextmanager

from redis.exceptions import RedisError
from domain.run_context import current_execution_id
from infra.runtime import RedisRuntime

DELTAS = {'llm.delta', 'agent.delta'}
BUSINESS_EVENTS = {
    'workflow.started', 'workflow.finished', 'workflow.failed', 'run.cancelled',
    'plan.generated', 'plan.replanned', 'wave.completed', 'plan.wave.completed',
    'plan.step.started', 'plan.step.observed', 'plan.step.failed', 'task.updated',
}
SUMMARY_FIELDS = ('event_id', 'version', 'category', 'scope_id', 'run_id', 'execution_id', 'agent_name',
                  'agent_id', 'conversation_id', 'message_id', 'name', 'created_at', 'trace')


def event_summary(event):
    if event.get('version') != 2 or event.get('name') in DELTAS:
        return None
    return {key: event[key] for key in SUMMARY_FIELDS if key in event}


def scope_delivery(event):
    if event.get('version') != 2:
        return None
    if event.get('trace'):
        # Status consumers still need these small identifiers, never execution bodies.
        return {**event_summary(event), 'payload': {k: v for k, v in event.get('payload', {}).items()
                if k in {'run_id', 'message_id', 'agent_id', 'status', 'cancelled', 'run', 'conversation_id'}}}
    return event


class EventStreamService:
    """Application-owned event classification; Redis only transports one channel."""

    def __init__(self, store, runtime: RedisRuntime | None = None):
        self._store = store
        self.runtime = runtime or RedisRuntime()
        self.runtime.journal.bind(store)
        self._subscribers = []

    def subscribe(self, callback):
        self._subscribers.append(callback)

    async def _broadcast(self, category, scope_id, event, *, durable=False):
        for attempt in range(3):
            try:
                return await asyncio.wait_for(self.runtime.append(category, scope_id, event, durable=durable), 5)
            except (RedisError, OSError, TimeoutError):
                if attempt < 2:
                    await asyncio.sleep((0.05, 0.15)[attempt])
                else:
                    raise

    def _event(self, run_id, name, payload):
        run = self._store.find_one('runs', {'run_id': run_id}) or {}
        return dict(event_id=str(uuid.uuid4()), version=2, category='run', run_id=run_id,
                    scope_id=run.get('scope_id') or run_id,
                    conversation_id=run.get('im_conversation_id') or run.get('conversation_id', ''),
                    message_id=run.get('source_message_id') or run.get('message_id', ''),
                    execution_id=payload.get('execution_id') or current_execution_id.get(),
                    agent_id=payload.get('agent_id') or payload.get('executor_id') or payload.get('planner_id', ''),
                    name=name, payload=payload, created_at=time.time())

    async def publish(self, run_id, name, payload):
        if name in BUSINESS_EVENTS:
            base = self._event(run_id, name, payload)
            event = await self.publish_scope(base['scope_id'], name, payload, context=base)
        else:
            event = self._event(run_id, name, payload)
            await self._broadcast('run', run_id, event, durable=True)
            # Only explicit product notifications are projected, never raw traces.
            if name.startswith(('artifacts.', 'human.confirmation.')):
                notification = dict(payload)
                notification.pop('arguments', None)
                await self.publish_scope(event['scope_id'], name, notification, context=event, trace=False)
        for callback in self._subscribers:
            result = callback(event)
            if inspect.isawaitable(result):
                await result
        return event

    async def publish_scope(self, scope_id, name, payload, *, context=None, trace=None):
        context = context or {}
        run_id = context.get('run_id') or payload.get('run_id') or (payload.get('run') or {}).get('run_id', '')
        run = self._store.find_one('runs', {'run_id': run_id}) if run_id else None
        run = run or {}
        trace = (name in BUSINESS_EVENTS or name.startswith(('agent.execution.', 'agent.reply.'))
                 or name == 'run.created') if trace is None else trace
        body = dict(payload)
        if trace:
            # Business state/links only. Model outputs stay in run events or messages.
            body = {k: v for k, v in body.items() if k in {
                'run_id', 'message_id', 'agent_id', 'conversation_id', 'scope_id', 'status',
                'cancelled', 'error', 'reason', 'mode', 'phase', 'step_id', 'execution_id',
            }}
            if name == 'run.created':
                body['run'] = {k: v for k, v in payload.get('run', {}).items() if k in {'run_id', 'status'}}
            plan = payload.get('plan') or {}
            if isinstance(plan, dict) and 'steps' in plan:
                body['steps'] = [{k: v for k, v in step.items() if k in {'step_id', 'title', 'executor_id', 'status'}}
                                 for step in plan['steps']]
            if isinstance(payload.get('step'), dict):
                body['step'] = {k: v for k, v in payload['step'].items()
                                if k in {'step_id', 'title', 'executor_id', 'status', 'status_reason'}}
        event = dict(event_id=str(uuid.uuid4()), version=2, category='scope',
                     scope_id=scope_id, room_id=scope_id, name=name, payload=body,
                     run_id=run_id, execution_id=context.get('execution_id') or current_execution_id.get(),
                     agent_id=context.get('agent_id') or payload.get('agent_id', ''),
                     agent_name=payload.get('agent_name', ''),
                     conversation_id=context.get('conversation_id') or run.get('im_conversation_id')
                     or payload.get('conversation_id') or run.get('conversation_id', ''),
                     message_id=context.get('message_id') or run.get('source_message_id')
                     or payload.get('message_id', ''), trace=trace, created_at=time.time())
        if run_id:
            body.setdefault('run_id', run_id)
        if event['message_id']:
            body.setdefault('message_id', event['message_id'])
        await self._broadcast('scope', scope_id, event, durable=True)
        return event

    async def no_store_publish(self, run_id, name, payload):
        event = self._event(run_id, name, payload)
        await self._broadcast('run', run_id, event)
        return event

    @asynccontextmanager
    async def execution(self, run_id, agent, *, phase='execute', step_id='', execution_id=None):
        token = current_execution_id.set(execution_id or str(uuid.uuid4()))
        payload = {'run_id': run_id, 'agent_id': agent.id, 'agent_name': getattr(agent, 'name', agent.id),
                   'phase': phase, 'step_id': step_id}
        context = self._event(run_id, 'agent.execution.started', payload)
        try:
            await self.publish_scope(context['scope_id'], 'agent.execution.started', payload, context=context)
            try:
                yield
            except asyncio.CancelledError:
                await self.publish_scope(context['scope_id'], 'agent.execution.cancelled', payload, context=context)
                raise
            except Exception as exc:
                await self.publish_scope(context['scope_id'], 'agent.execution.failed', {**payload, 'error': str(exc)}, context=context)
                raise
            else:
                failed = getattr(agent, 'states', {}).get('is_finished') is False and phase == 'execute'
                await self.publish_scope(context['scope_id'], 'agent.execution.failed' if failed else 'agent.execution.finished',
                                         payload, context=context)
        finally:
            current_execution_id.reset(token)

    async def list_events(self, run_id, *, user_id='service'):
        return await self.runtime.journal.events('run', run_id, user_id=user_id)

    async def summaries(self, run_id, *, execution_id=None, after=None, limit=100, user_id='service'):
        events = await self.list_events(run_id, user_id=user_id)
        anchor = next((e for e in events if e['event_id'] == after), None) if after else None
        if after and anchor is None:
            raise KeyError('事件游标不存在')
        items = [e for e in events if event_summary(e) is not None
                 and (not execution_id or e.get('execution_id') == execution_id)
                 and (not anchor or (e.get('created_at', 0), e['event_id']) >
                      (anchor.get('created_at', 0), anchor['event_id']))]
        return {'items': [event_summary(e) for e in items[:limit]],
                'next_cursor': items[limit - 1]['event_id'] if len(items) > limit else None}

    async def get_event(self, run_id, event_id, *, user_id='service'):
        return next((e for e in await self.list_events(run_id, user_id=user_id) if e['event_id'] == event_id), None)

    async def stream(self, run_id, last_id=None, *, summary=False, execution_id=None, user_id='service'):
        def transform(event):
            if execution_id and event.get('execution_id') != execution_id:
                return None
            return event_summary(event) if summary else event
        async for raw in self.runtime.stream('run', run_id,
                lambda: self.runtime.journal.history('run', run_id), last_id=last_id,
                transform=transform, user_id=user_id):
            yield raw

    def format_sse(self, event, cursor=None):
        return self.runtime.sse(event, cursor)
