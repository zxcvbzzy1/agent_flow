import asyncio
from functools import wraps
from typing import Any, Callable, List
from domain.event import Event
import inspect
from infra.eventbus import EventBus


CallBack = Callable[[Event,Callable], Any]

class On_bind():

    _instance = None
    _instance_bool = False

    def __new__(cls, *args, **kwargs):
        if not cls._instance_bool:
            cls._instance = super().__new__(cls, *args, **kwargs)
        return cls._instance
    def __init__(self):
        if self.__class__._instance_bool:
            return
        self._bus = EventBus()
        self.__class__._instance_bool = True

    # 单事件匹配
    def on(self,event:Event):
        def decorator(fn):
            is_coro = inspect.iscoroutinefunction(fn)
            @wraps(fn)
            async def wrapper(event2, *args, **kwargs):
                new_kwargs = {**event2.unpack(), **kwargs}

                result = await fn(*args, **new_kwargs) if is_coro else fn(*args, **new_kwargs)
                if isinstance(result, Event):
                    await self._bus.publish(result)
                return result
            
            self._bus.subscribe(event.name, wrapper)
            return wrapper

        return decorator
    # 模式事件匹配
    def on_pattern(self, pattern: str) -> Callable:
        def decorator(fn: Callable) -> Callable:
            is_coro = inspect.iscoroutinefunction(fn)
            @wraps(fn)
            async def wrapper(event, *args, **kwargs):
                new_kwargs = {**event.unpack(), **kwargs}
                result = await fn(*args, **new_kwargs) if is_coro else fn(*args, **new_kwargs)
                if isinstance(result, Event):
                    await self._bus.publish(result)
                return result

            self._bus.register_pattern(pattern, wrapper)
            return fn
        return decorator

    # 中间件绑定
    def use(self,):
        def decorator(fn:CallBack):
            self._bus.use(fn)
        return decorator
    
    def get_handlers_events(self):
        return self._bus.registered_events()
    
    def get_middlewares(self):
        return self._bus.get_middlewares()