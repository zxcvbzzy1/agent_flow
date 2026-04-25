"""
所有 ContextProvider 的具体实现。

分两类：
  静态 Provider  —— 数据来自 state dict，不依赖上下文管理
  动态 Provider  —— 数据来自记忆，由上下文管理

Provider 只格式化，不做任何存储或管理决策。
"""

from __future__ import annotations
from abc import ABC, abstractmethod

from domain.context.strategy import ContextStrategy, FullHistoryStrategy,ContextItem
from domain.memory.short.default_short_term_memory import ShortTermMemory


# ── Provider 基类 ────────────────────────────────────────────────────

class ContextProvider(ABC):
    name:    str
    enabled: bool = True

    def disable(self): self.enabled = False
    def enable(self):  self.enabled = True

    @abstractmethod
    def get(self, state: dict) -> list[str]:
        ...


# ── 动态需要 memory 的 Provider 基类 ────────────────────────────────

class MemoryProvider(ContextProvider, ABC):
    """从 ShortTermMemory 经 Strategy 取 items，再格式化。"""

    def __init__(
        self,
        memory:   ShortTermMemory,
        strategy: ContextStrategy | None = None,
    ) -> None:
        self._memory   = memory
        self._strategy = strategy or FullHistoryStrategy()

    def _get_items(self, state: dict) -> list[ContextItem]:
        return self._strategy.apply(self._memory, state)


# ── 具体 Provider ─────────────────────────────────────────────────

class ToolOutputProvider(MemoryProvider):
    name = "tool_output"

    def get(self, state: dict) -> list[str]:
        items = self._get_items(state)
        if not items:
            return []
        parts = [f"## 工具反馈（{len(items)} 条）"]
        for item in items:
            parts.append(f"### {item.source}\n{item.content}")
            if item.metadata.get("summarized"):
                parts.append(
                    f'（内容已压缩，调用 explore_context("{item.source}") 获取原文）'
                )
        return ["\n\n".join(parts)]


class HistoryProvider(MemoryProvider):
    name = "history"

    def get(self, state: dict) -> list[str]:
        items = self._get_items(state)
        if not items:
            return []
        parts = ["## 对话历史"]
        parts += [item.content for item in items]
        return ["\n".join(parts)]




class UserPromptProvider(ContextProvider):
    """任务入口，注入用户原始需求。"""
    name = "user"

    def get(self, state: dict) -> list[str]:
        text = (
            f"请开始处理以下需求：\n"
            f"用户需求：{state.get('prompt', '')}\n"
            f"根据需求，决定下一步调用哪个工具。"
        )
        return [text]


class StateProvider(ContextProvider):
    """当前执行状态：重试次数、工具调用历史、失败提示。"""
    name = "task"

    def get(self, state: dict) -> list[str]:
        parts = ["## 当前执行状态"]
        if state.get("retry", 0) > 0:
            parts.append(f"- 已重试：{state['retry']} 次")
        if not state.get("last_tool_ok", True):
            parts.append("- 上一个工具执行失败，请决定是否重试或换其他工具")
        if state.get("tool_history"):
            parts.append(f"- 已调用工具：{' -> '.join(state['tool_history'])}")
        parts.append("请决定下一步调用哪个工具，或输出 is_finished=true。")
        return ["\n".join(parts)]


class AvailableToolsProvider(ContextProvider):
    name = "available_tools"

    def __init__(self, available_fields: list[str]) -> None:
        self._fields = available_fields

    def get(self, state: dict) -> list[str]:
        import json
        from domain.tool import Tool
        lines = ["当前可用工具："]
        for tool in Tool.get_all_tools():
            if tool.field in self._fields:
                lines.append(
                    tool.name + "\n"
                    + tool.description + "\n"
                    + json.dumps(tool.input_schema, ensure_ascii=False)
                )
        return ["\n".join(lines)] if len(lines) > 1 else []