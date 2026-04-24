"""
domain/context/providers.py

所有 ContextProvider 的具体实现。

分两类：
  静态 Provider  —— 数据来自 state dict，不依赖 ContextStore
                    （UserPromptProvider、StateProvider、AvailableToolsProvider）
  动态 Provider  —— 数据来自 ContextStore.window()，只负责格式化
                    （ToolRespondProvider、ExploredContextProvider、HistoryProvider）

Provider 只格式化，不做任何存储或管理决策。
"""
from __future__ import annotations

import json

from domain.context.context import ContextProvider, ContextSlot
from domain.context.store.store import ContextStore
from domain.tool import Tool


# ---------------------------------------------------------------------------
# Slot 声明（每个 Provider 对应一个 Slot）
# ---------------------------------------------------------------------------

SLOT_USER_PROMPT = ContextSlot(
    name="user_prompt",
    description="用户原始需求，任务入口",
    scope="task",
)

SLOT_STATE = ContextSlot(
    name="state",
    description="当前执行状态：retry、phase、tool_history",
    scope="state",
)

SLOT_HISTORY = ContextSlot(
    name="history",
    description="多轮对话历史，来自 ContextStore history scope",
    scope="history",
)

SLOT_TOOL_RESPOND = ContextSlot(
    name="tool_respond",
    description="工具执行结果，来自 ContextStore memory scope",
    scope="memory",
)

SLOT_EXPLORED = ContextSlot(
    name="explored_context",
    description="Agent 主动 explore 后 promote 进窗口的内容",
    scope="memory",
)

SLOT_AVAILABLE_TOOLS = ContextSlot(
    name="available_tools",
    description="当前可用工具列表及 schema",
    scope="tool",
)


# ---------------------------------------------------------------------------
# 静态 Provider：数据来自 state dict
# ---------------------------------------------------------------------------

class UserPromptProvider(ContextProvider):
    """任务入口，注入用户原始需求。"""
    slot = SLOT_USER_PROMPT

    def get(self, state: dict) -> list[str]:
        text = (
            f"请开始处理以下需求：\n"
            f"用户需求：{state.get('prompt', '')}\n"
            f"根据需求，决定下一步调用哪个工具。"
        )
        return [text]


class StateProvider(ContextProvider):
    """当前执行状态：重试次数、工具调用历史、失败提示。"""
    slot = SLOT_STATE

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
    """可用工具列表，按 field 过滤。"""
    slot = SLOT_AVAILABLE_TOOLS

    def __init__(self, available_fields: list[str]) -> None:
        self._fields = available_fields

    def get(self, state: dict) -> list[str]:
        lines = ["\n当前可用工具："]
        for tool in Tool.get_all_tools():
            if tool.field in self._fields:
                lines.append(
                    tool.name + "\n"
                    + tool.description + "\n"
                    + json.dumps(tool.input_schema, ensure_ascii=False)
                )
        if len(lines) == 1:
            return []
        return ["\n".join(lines)]


# ---------------------------------------------------------------------------
# 动态 Provider：数据来自 ContextStore.window()
# ---------------------------------------------------------------------------

class ToolRespondProvider(ContextProvider):
    """
    格式化 ContextStore memory scope 的 promoted 节点。

    skeleton 节点   → 展示结构预览 + explore 提示
    chunk 节点      → 展示片段内容 + 编号
    full 节点       → 直接展示全文

    """
    slot = SLOT_TOOL_RESPOND

    def __init__(self, store: ContextStore) -> None:
        self._store = store

    def get(self, state: dict) -> list[str]:
        nodes = self._store.window(scope="memory")
        if not nodes:
            return []

        parts = [f"## 工具反馈（{len(nodes)} 条）"]
        for node in nodes:
            if node.is_skeleton():
                parts.append(
                    f"### {node.label()}\n{node.content}"
                )
            elif node.is_chunk():
                parts.append(
                    f"### {node.label()}\n{node.content}"
                )
            else:
                parts.append(
                    f"### {node.source_key}\n{node.content}"
                )

        return ["\n\n".join(parts)]


class ExploredContextProvider(ContextProvider):
    """
    展示 Agent 通过 explore() 主动 promote 的节点。

    这些节点在 metadata 里带有 explored=True 标记，
    与工具输出节点同在 memory scope，但单独成一个 section
    便于 LLM 识别"这是我主动要求加载的内容"。
    """
    slot = SLOT_EXPLORED

    def __init__(self, store: ContextStore) -> None:
        self._store = store

    def get(self, state: dict) -> list[str]:
        nodes = [
            n for n in self._store.window(scope="memory")
            if n.metadata.get("explored")
        ]
        if not nodes:
            return []

        pieces: list[str] = []
        for node in nodes:
            pieces.append(
                f"### 探索内容：{node.label()}\n{node.content}"
            )
        return pieces


class HistoryProvider(ContextProvider):
    """
    格式化 ContextStore history scope 的 promoted 节点。
    旧的对话轮次被 Store demote 后自然消失，无需在 Provider 里处理。
    """
    slot = SLOT_HISTORY

    def __init__(self, store: ContextStore) -> None:
        self._store = store

    def get(self, state: dict) -> list[str]:
        nodes = self._store.window(scope="history")
        if not nodes:
            return []
        parts = ["## 对话历史"]
        for node in nodes:
            parts.append(node.content)
        return ["\n".join(parts)]