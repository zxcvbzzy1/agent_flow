# domain/context/strategy.py
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable
from domain.memory.short.short_term_memory import ShortTermMemory, memory_field



@dataclass
class ContextItem:
    """
    Strategy 处理后交给 Provider 的中间结构，不持久化。
    source  : 来源标识，如 "tool:search#1"
    content : 实际注入 prompt 的文本
    metadata: 透传的额外信息，Provider 可按需使用
    """
    source:   str
    content:  str
    metadata: dict = field(default_factory=dict)

    @property
    def tokens(self) -> int:
        return max(1, len(self.content))

class ContextStrategy(ABC):
    """
    从 ShortTermMemory 取原文，处理后返回 ContextItem 列表。
    支持 | 运算符串联成 Pipeline。
    """

    @abstractmethod
    def apply(self, memory: ShortTermMemory, field: memory_field, state: dict) -> list[ContextItem]:
        ...

    def __or__(self, other: "ContextStrategy") -> "StrategyPipeline":
        return StrategyPipeline([self, other])


class ItemStrategy(ContextStrategy, ABC):
    """
    只对已有 ContextItem 列表做变换，不再读 memory。
    Pipeline 中第二个及之后的策略应继承此类。
    """

    def apply(self, memory: ShortTermMemory, field: memory_field, state: dict) -> list[ContextItem]:
        # 单独使用时从 memory 拿指定 field 的原文再变换
        items = [
            ContextItem(source=f"{key}#{i+1}", content=raw, metadata={"field": field, "name": key})
            for key in memory.keys_by_field(field)
            for i, raw in enumerate(
                [memory.get(field, key, j+1) for j in range(memory.count(field, key))]
            )
            if raw
        ]
        return self.transform(items, state)

    @abstractmethod
    def transform(self, items: list[ContextItem], state: dict) -> list[ContextItem]:
        ...

class StrategyPipeline(ContextStrategy):
    """
    串联多个策略，前者输出作为后者输入。
    注意：Pipeline 内第一个策略从 memory 读取，
    后续策略接收上一个策略产出的 ContextItem 列表。
    """

    def __init__(self, strategies: list[ContextStrategy]) -> None:
        self._strategies = strategies

    def apply(self, memory: ShortTermMemory, field: memory_field, state: dict) -> list[ContextItem]:
        # 第一个策略从 memory 产出初始 items
        items = self._strategies[0].apply(memory, field, state)
        # 后续策略对 items 做变换，用 ItemStrategy 包装
        for s in self._strategies[1:]:
            if isinstance(s, ItemStrategy):
                items = s.transform(items, state)
            else:
                # 非 ItemStrategy 重新从 memory 读，结果追加
                items = s.apply(memory, field, state)
        return items

    def __or__(self, other: "ContextStrategy") -> "StrategyPipeline":
        return StrategyPipeline([*self._strategies, other])





# ── 内置策略 ─────────────────────────────────────────────────────

class FullHistoryStrategy(ContextStrategy):
    """透传所有输出原文，不做任何处理。"""

    def apply(self, memory: ShortTermMemory, field: memory_field, state: dict) -> list[ContextItem]:
        items: list[ContextItem] = []

        for key in memory.keys_by_field(field):
            for i in range(memory.count(field, key)):
                raw = memory.get(field, key, i + 1)
                if raw:
                    items.append(ContextItem(
                        source=f"{key}#{i+1}",
                        content=raw,
                        metadata={"field": field, "name": key, "call_index": i + 1},
                    ))
        return items


class LatestOnlyStrategy(ContextStrategy):
    """只取最新一次输出。"""

    def apply(self, memory: ShortTermMemory, field: memory_field, state: dict) -> list[ContextItem]:
        items: list[ContextItem] = []
        for key in memory.keys_by_field(field):
            raw = memory.get(field, key, 0)
            if raw:
                items.append(ContextItem(
                    source=f"{key}#latest",
                    content=raw,
                    metadata={"field": field, "name": key},
                ))
        return items


# 中间策略
class TokenBudgetStrategy(ItemStrategy):
    """超出 token 上限时从最旧的 item 开始丢弃。"""

    def __init__(self, token_limit: int) -> None:
        self._limit = token_limit

    def transform(self, items: list[ContextItem], state: dict) -> list[ContextItem]:
        if sum(i.tokens for i in items) <= self._limit:
            return items
        # 从最旧（列表头部）开始丢
        kept = list(items)
        while kept and sum(i.tokens for i in kept) > self._limit:
            kept.pop(0)
        return kept


class RecencyStrategy(ItemStrategy):
    """只保留最近 N 条 item。"""

    def __init__(self, keep_last: int) -> None:
        self._n = keep_last

    def transform(self, items: list[ContextItem], state: dict) -> list[ContextItem]:
        return items[-self._n:]


class SummarizeStrategy(ItemStrategy):
    """
    超长 item 替换为摘要。
    outline_fn: (source, content) -> summary_str
    为 None 时降级为截断。
    """

    def __init__(
        self,
        threshold:  int = 800,
        outline_fn: Callable[[str, str], str] | None = None,
    ) -> None:
        self._threshold = threshold
        self._outline_fn = outline_fn

    def transform(self, items: list[ContextItem], state: dict) -> list[ContextItem]:
        result: list[ContextItem] = []
        for item in items:
            if len(item.content) <= self._threshold:
                result.append(item)
            else:
                summary = (
                    self._outline_fn(item.source, item.content)
                    if self._outline_fn
                    else item.content[:self._threshold] + "\n\n[内容已截断]"
                )
                result.append(ContextItem(
                    source=item.source,
                    content=summary,
                    metadata={**item.metadata, "summarized": True},
                ))
        return result


class FilterByToolStrategy(ItemStrategy):
    """只保留指定工具名的 item。"""

    def __init__(self, names: list[str]) -> None:
        self._names = set(names)

    def transform(self, items: list[ContextItem], state: dict) -> list[ContextItem]:
        return [
            i for i in items
            if i.metadata.get("name") in self._names
        ]

