"""
domain/context/context_engine.py

ContextEngine：把所有 Provider 的输出拼成最终注入 LLM 的字符串。

层级关系
--------
ContextStore  →  window()  →  ContextProvider.get()  →  ContextEngine.build()  →  LLM prompt


Provider 是 Store 的只读格式化视图，不做任何管理决策。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal

from domain.context.store.store import ContextStore

SlotScope = Literal[
    "task",     # 任务级：用户 prompt、目标
    "state",    # 状态级：retry、phase、tool_history
    "memory",   # 记忆级：工具输出、探索内容
    "tool",     # 工具级：可用工具列表
    "history",  # 历史级：多轮对话
    "child",    # 子 agent 隔离上下文
]


# ---------------------------------------------------------------------------
# ContextSlot：Provider 的身份描述符
# ---------------------------------------------------------------------------

@dataclass
class ContextSlot:
    """
    描述一个 Provider 提供什么内容、属于哪个 scope。
    是 Provider 的声明式元数据，不携带处理策略。

    enabled     : False 时 ContextEngine 跳过此 Provider
    """
    name:        str
    description: str
    scope:       SlotScope
    enabled:     bool = True
    metadata:    dict = field(default_factory=dict)

    def disable(self) -> None: self.enabled = False
    def enable(self)  -> None: self.enabled = True



# ---------------------------------------------------------------------------
# ContextProvider 抽象基类
# ---------------------------------------------------------------------------

class ContextProvider(ABC):
    """
    只负责格式化，不做存储和管理决策。
    子类必须声明 slot 类属性。
    """
    slot: ContextSlot  # 子类声明

    @abstractmethod
    def get(self, state: dict) -> list[str]:
        """
        从数据源（state dict 或 ContextStore.window()）读取内容，
        格式化为 ContextPiece 列表。
        """
        ...


# ---------------------------------------------------------------------------
# ComposeStrategy：把所有 ContextPiece 拼成最终字符串
# ---------------------------------------------------------------------------

class ComposeStrategy(ABC):
    @abstractmethod
    def compose(self, pieces: list[str]) -> str:
        ...


class DefaultComposeStrategy(ComposeStrategy):
    """按 piece 顺序用双换行拼接，忽略空内容"""
    def compose(self, pieces: list[str]) -> str:
        return "\n\n".join(p for p in pieces if p.strip())




class ContextEngine:
    """
    遍历 providers，调用 get()，用 strategy 拼接成最终 prompt 字符串。

    Parameters
    ----------
    providers : Provider 列表，顺序即注入顺序
    strategy  : 拼接策略，默认 DefaultComposeStrategy
    """

    def __init__(
        self,
        providers: list[ContextProvider],
        context_store:ContextStore =None,
        strategy:  ComposeStrategy | None = None,
    ) -> None:
        self._providers = providers
        self._strategy  = strategy or DefaultComposeStrategy()
        self._context_store = context_store

    def build(self, state: dict) -> str:
        all_pieces: list[str] = []

        for provider in self._providers:
            slot = getattr(provider, "slot", None)
            if slot and not slot.enabled:
                continue
            try:
                pieces = provider.get(state)
                if pieces:
                    all_pieces.extend(pieces)
            except Exception as e:
                name = slot.name if slot else type(provider).__name__
                print(f"[ContextEngine] provider '{name}' error: {e}")

        return self._strategy.compose(all_pieces)
    
    def get_store(self):
        return self._context_store