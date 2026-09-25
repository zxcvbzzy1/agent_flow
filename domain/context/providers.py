"""
所有 ContextProvider 的具体实现。

分两类：
  静态 Provider  —— 数据来自 state dict，不依赖上下文管理
  动态 Provider  —— 数据来自记忆，由上下文管理

Provider 只格式化，不做任何存储或管理决策。
"""

from __future__ import annotations
from abc import ABC, abstractmethod

from domain.context.strategy import ContextStrategy, FullHistoryStrategy, ConsumeOnceStrategy, ContextItem
from domain.memory.short.default_short_term_memory import ShortTermMemory
from domain.memory.short.short_term_memory import memory_field
import json
from domain.tool import Tool

# ── Provider 基类 ────────────────────────────────────────────────────

class ContextProvider(ABC):
    name:    str
    enabled: bool = True

    @classmethod
    def disable(cls): cls.enabled = False
    @classmethod
    def enable(cls):  cls.enabled = True

    @abstractmethod
    def get(self, state: dict) -> list[str]:
        ...


# ── 动态需要 memory 的 Provider 基类 ────────────────────────────────

class MemoryProvider(ContextProvider, ABC):
    """从 ShortTermMemory 经 Strategy 取 items，再格式化。"""

    def __init__(
        self,
        memory:   ShortTermMemory,
        field:    memory_field,
        strategy: ContextStrategy | None = None,
    ) -> None:
        self._memory   = memory
        self._field    = field
        self._strategy = strategy or FullHistoryStrategy()

    def _get_items(self, state: dict) -> list[ContextItem]:
        return self._strategy.apply(self._memory, self._field, state)


# ── 具体 Provider ─────────────────────────────────────────────────

# 静态的provider

class UserPromptProvider(ContextProvider):
    """任务入口，注入用户原始需求。"""
    name = "user"

    def get(self, state: dict) -> list[str]:
        text = (
            f"请开始处理以下需求：\n"
            f"用户需求：{state.get('prompt', '')}\n"
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
        return ["\n".join(parts)]


class AvailableToolsProvider(ContextProvider):
    name = "available_tools"

    def __init__(
        self,
        available_fields: list[str] | None = None,
        available_tools: list[str] | None = None,
    ) -> None:
        # 按 field 分组粗选，或按具体工具名细选；二者取并集。
        self._fields = list(available_fields or [])
        self._tools = list(available_tools or [])

    def get(self, state: dict) -> list[str]:

        lines = ["当前可用工具："]
        for tool in Tool.get_all_tools():
            if tool.field in self._fields or tool.name in self._tools:
                lines.append(
                    tool.name + "\n"
                    + tool.description + "\n"
                    + json.dumps(tool.input_schema, ensure_ascii=False) + "\n"
                )
        return ["\n".join(lines)] if len(lines) > 1 else []


class PinnedContextProvider(ContextProvider):
    """用户收藏的关键信息，作为长期固定上下文注入。

    数据来自 state["pinned_context"]（list[str]），由应用层在每次运行前写入。
    与历史/工具反馈不同，这里是"固定写入"，不参与裁剪/摘要策略。
    """
    name = "pinned_context"

    def get(self, state: dict) -> list[str]:
        items = state.get("pinned_context") or []
        cleaned = [str(item).strip() for item in items if str(item).strip()]
        if not cleaned:
            return []
        parts = ["## 固定上下文（用户收藏，长期有效）"]
        parts += [f"- {item}" for item in cleaned]
        return ["\n".join(parts)]


# 动态provider

class ReActToolFeedbackProvider(MemoryProvider):
    """ReACT 工具反馈：逐条渲染已在 memory 里拼好的完整 ReACT 块
    （Thought→Action→Args→Observation），让模型在下一轮看到自己当初的
    思考与行动，而不只是孤立的 Observation —— 从根上避免重复调用同一工具。
    """
    name = "tool_output"

    def get(self, state: dict) -> list[str]:
        items = self._get_items(state)
        if not items:
            return []
        parts = [f"## ReACT 工具反馈（{len(items)} 条）：思考→行动→观察"]
        for item in items:
            # item.content 已是完整 ReACT 块（由 AgentBase._build_react_block 生成）
            parts.append(f"### {item.source}\n{item.content}")
            if item.metadata.get("summarized"):
                parts.append(
                    f'（内容已压缩）'
                )
        return ["\n\n".join(parts)]


# 别名：保留旧名以兼容大量已有 import / isinstance 检查（指向同一类对象，
# isinstance(provider, ToolOutputProvider) 仍正确）。
ToolOutputProvider = ReActToolFeedbackProvider


class HistoryProvider(MemoryProvider):
    name = "history"

    def get(self, state: dict) -> list[str]:
        items = self._get_items(state)
        if not items:
            return []
        parts = ["## 对话历史"]
        parts += [item.content for item in items]
        return ["\n".join(parts)]


class ErrorProvider(MemoryProvider):
    """错误回灌：注入上一轮解析失败/不合规输出等错误信息，提醒模型纠正。

    默认使用 ConsumeOnceStrategy —— 错误只注入一次，注入后即从 memory 删除，
    因此下一轮不再出现（除非又产生了新错误）。无错误时输出为空，无副作用。
    """
    name = "error"

    def __init__(
        self,
        memory:   ShortTermMemory,
        field:    memory_field = "error",
        strategy: ContextStrategy | None = None,
    ) -> None:
        super().__init__(memory, field, strategy or ConsumeOnceStrategy())

    def get(self, state: dict) -> list[str]:
        items = self._get_items(state)
        if not items:
            return []
        parts = ["## 上一轮错误（请修正后重试，本提示仅出现一次）"]
        parts += [item.content for item in items]
        return ["\n\n".join(parts)]




class SkillProvider(MemoryProvider):
    """召回技能注入：从 memory 的 "skill" 字段取出已召回的技能块再注入。

    技能作为“记忆”存储（每条技能一个 memory 条目，key=skill_id，content=已格式化的技能块），
    由系统检索召回（AgentBase._recall_skills）与 recall_skill 工具共同写入。这里只负责取出
    并格式化；memory 中无技能时输出为空，对未召回的 Agent（如 planner）完全透明。
    """

    name = "skill"

    def __init__(
        self,
        memory:   ShortTermMemory,
        field:    memory_field = "skill",
        strategy: ContextStrategy | None = None,
    ) -> None:
        super().__init__(memory, field, strategy or FullHistoryStrategy())

    def get(self, state: dict) -> list[str]:
        items = self._get_items(state)
        if not items:
            return []
        parts = ["## 召回技能（Skills）——可直接参考其中的方法/步骤完成当前任务"]
        parts += [item.content for item in items if str(item.content or "").strip()]
        return ["\n\n".join(parts)] if len(parts) > 1 else []

