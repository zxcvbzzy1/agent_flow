import asyncio
from dataclasses import dataclass
import fnmatch
from typing import Callable, Any, Optional
from domain.event import Event, EventBusPort


class EventBus(EventBusPort):
    """
    通用事件总线（单例）。
    
    职责：
    - subscribe(event_name, handler)  订阅事件
    - publish(event)                  发布事件（异步）
    - publish_sync(event)             发布事件（同步）
    
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init()
        return cls._instance

    def _init(self):
        # event_name -> [handler, ...]
        self._handlers: dict[str, list[Callable]] = {}
        # 全局中间件，对所有事件生效
        self._middlewares: list[Callable] = []
        # 模式匹配
        self._pattern_handlers:list[tuple[str, Callable]] = []


    # ── 订阅 ────────────────────────────────────────────────────────

    def subscribe(self, event_name: str, handler: Callable) -> None:
        """
        订阅单个事件名。
        handler 签名：async def handler(event: Event) -> Any
        """
        self._handlers.setdefault(event_name, []).append(handler)

    def unsubscribe(self, event_name: str, handler: Callable) -> None:
        """取消订阅。"""
        handlers = self._handlers.get(event_name, [])
        if handler in handlers:
            handlers.remove(handler)

    def use(self, middleware: Callable) -> Callable:
        """
        注册全局中间件。
        middleware 签名：async def mw(event: Event, call_next: Callable) -> Any
        """
        self._middlewares.append(middleware)
        return middleware
    
    def register_pattern(self, pattern: str, handler: Callable) -> None:
        """注册通配符 handler。"""
        self._pattern_handlers.append((pattern, handler))

    # ── 发布 ────────────────────────────────────────────────────────

    async def publish(self, event: Event) -> list[Any]:
        """
        发布事件，依次执行所有订阅的 handler。
        返回所有 handler 的执行结果列表。
        """
        handlers = self._handlers.get(event.name, [])

        results = []
        if handlers:
            for handler in handlers:
                try:
                    result = await self._run_with_middleware(event, handler)
                    results.append(result)
                except Exception as e:
                    print(f"[ERROR] handler failed: {e}")
                    results.append(e)
            return results

        for pattern, handler in self._pattern_handlers:
            if fnmatch.fnmatch(event.name, pattern):
                try:
                    result = await self._run_with_middleware(event, handler)
                    results.append(result)
                except Exception as e:
                    print(f"[ERROR] handler failed: {e}")
                    results.append(e)

        return results
    
    async def publish_one(self, event: Event) -> Any:
        """发布事件，只返回第一个 handler 的结果（单处理器场景）。"""
        results = await self.publish(event)
        return results[0] if results else None

    def publish_sync(self, event: Event) -> list[Any]:
        """同步发布（在非 async 上下文中使用）。"""
        try:
            loop = asyncio.get_running_loop()
            # 已有事件循环：创建 Task
            return loop.create_task(self.publish(event))
        except RuntimeError:
            # 没有事件循环：直接 run
            return asyncio.run(self.publish(event))

    # ── 中间件 ───────────────────────────────────────────────────────

    async def _run_with_middleware(self, event: Event, handler: Callable) -> Any:

        async def call_next(index: int):
            if index == len(self._middlewares):
                return await handler(event)

            mw = self._middlewares[index]

            async def next_func():
                return await call_next(index + 1)

            return await mw(event, next_func)

        return await call_next(0)

    # ── 调试 ─────────────────────────────────────────────────────────

    def registered_events(self) -> list[str]:
        """查看当前所有已注册的事件名。"""
        return sorted(self._handlers.keys())

    def get_middlewares(self) -> list[Callable]:
        """查看当前所有已注册的中间件。"""
        return self._middlewares

    def clear(self) -> None:
        """清空所有订阅（测试用）。"""
        self._handlers.clear()
        self._middlewares.clear()