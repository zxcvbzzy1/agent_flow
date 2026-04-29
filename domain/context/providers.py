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

    def __init__(self, available_fields: list[str]) -> None:
        self._fields = available_fields

    def get(self, state: dict) -> list[str]:

        lines = ["当前可用工具："]
        for tool in Tool.get_all_tools():
            if tool.field in self._fields:
                lines.append(
                    tool.name + "\n"
                    + tool.description + "\n"
                    + json.dumps(tool.input_schema, ensure_ascii=False) + "\n"
                )
        return ["\n".join(lines)] if len(lines) > 1 else []



class PlanProvider(ContextProvider):
    """当前计划状态，供 Agent 决策下一步。"""
    name = "plan"

    def get(self, state: dict) -> list[str]:
        plan_dict = state.get("plan", {})

        # 无计划时，提示 Agent 先制定计划
        if not plan_dict or not plan_dict.get("steps"):
            return ["请决定下一步调用哪个工具，或输出 is_finished=true。"]

        steps    = plan_dict.get("steps", [])
        finished = plan_dict.get("finished", False)
        summary  = plan_dict.get("summary", "")

        # 状态符号映射
        STATUS_ICON = {
            "pending":     "⬜",
            "in_progress": "🔄",
            "done":        "✅",
            "failed":      "❌",
            "skipped":     "⏭️",
        }

        parts = ["## 当前计划"]

        # 统计进度
        total        = len(steps)
        done_count   = sum(1 for s in steps if s["status"] == "done")
        failed_count = sum(1 for s in steps if s["status"] == "failed")
        parts.append(f"进度：{done_count}/{total} 已完成"
                     + (f",{failed_count} 个失败" if failed_count else ""))

        # 步骤列表
        parts.append("")
        for s in steps:
            icon   = STATUS_ICON.get(s["status"], "⬜")
            note   = f"  ↳ {s['note']}" if s.get("note") else ""
            detail = f"\n     {s['detail']}" if s.get("detail") else ""
            parts.append(f"{icon} [{s['step_id']}] {s['title']}{detail}{note}")

        # 下一步提示
        parts.append("")
        if finished:
            parts.append(f"计划已全部完成。\n总结：{summary}")
        else:
            # 找到第一个 pending 或 in_progress 的步骤
            next_step = next(
                (s for s in steps if s["status"] in ("pending", "in_progress")),
                None,
            )
            if next_step:
                parts.append(
                    f"▶ 下一步：[{next_step['step_id']}] {next_step['title']}\n"
                    f"请调用对应工具执行，完成后调用 update_plan 更新状态。"
                )
            else:
                parts.append("所有步骤已处理，请调用 finish_plan 完成计划。")

        return ["\n".join(parts)]


# 动态provider

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




