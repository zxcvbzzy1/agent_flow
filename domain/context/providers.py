# domain/context/providers.py

from domain.context.context import ContextProvider
from domain.memory.short.short_term_memory import ShortTermMemory
from domain.tool import Tool
import json

RECENT_COUNT = 5


class UserPromptProvider(ContextProvider):
    def get(self, state: dict) -> str:
        return (
            f"请开始处理以下需求：\n"
            f"用户需求：{state.get('prompt', '')}\n"
            f"根据需求，决定下一步调用哪个工具。"
        )


class StateProvider(ContextProvider):
    def get(self, state: dict) -> str:
        parts = ["## 当前执行状态"]
        if state.get("retry", 0) > 0:
            parts.append(f"- 已重试：{state['retry']} 次")
        if not state.get("last_tool_ok", True):
            parts.append("- ⚠️ 上一个工具执行失败，请决定是否重试或换其他工具")
        if state.get("tool_history"):
            parts.append(f"- 已调用工具：{' -> '.join(state['tool_history'])}")
        parts.append("请决定下一步调用哪个工具，或输出 is_finished=true。")
        return "\n".join(parts)


class HistoryProvider(ContextProvider):
    def get(self, state: dict) -> str:
        history = state.get("history", [])
        if not history:
            return ""
        return "## 历史记录\n" + "\n".join(history)


class ToolRespondProvider(ContextProvider):
    """把记忆摘要格式化为 prompt，不依赖 agent"""
    def __init__(self, memory: ShortTermMemory, recent_count: int = RECENT_COUNT):
        self._memory = memory
        self._recent_count = recent_count

    def get(self, state: dict) -> str:
        summary_list = self._memory.get_summary_list()
        if not summary_list:
            return ""
        total = len(summary_list)
        parts = [f"- 工具反馈（共 {total} 条）："]
        for i, item in enumerate(summary_list):
            tool_name = item["tool_name"]
            summary   = item["summary"]
            index     = item["index"]
            is_recent = i >= total - self._recent_count
            if is_recent:
                parts.append(f"  [{tool_name} 第{index}次] {summary}")
            else:
                parts.append(
                    f"  [{tool_name} 第{index}次] (已折叠) "
                    f"→ 如需使用请填 $ref:{tool_name}#{index}"
                )
        return "\n".join(parts)


class RefHintProvider(ContextProvider):
    """提示 LLM 可引用的工具结果，不依赖 agent"""
    def __init__(self, memory: ShortTermMemory):
        self._memory = memory

    def get(self, state: dict) -> str:
        keys = self._memory.all_keys()
        if not keys:
            return ""
        lines = ["- 以下工具结果已缓存，如需使用请直接用 $ref 引用："]
        for tool_name in keys:
            count = self._memory.count(tool_name)
            for i in range(1, count + 1):
                lines.append(f"  $ref:{tool_name}#{i}  ← {tool_name} 第{i}次完整输出")
        return "\n".join(lines)


class AvailableToolsProvider(ContextProvider):
    """列出可用工具，只依赖 Tool 注册表和 fields 过滤条件"""
    def __init__(self, avilable_fields: list[str]):
        self._fields = avilable_fields

    def get(self, state: dict) -> str:
        return "\n当前可用工具、简介和其参数：\n" + "\n".join([
            tool.name + '\n' + tool.description + '\n' +
            json.dumps(tool.input_schema, ensure_ascii=False)
            for tool in Tool.get_all_tools()
            if tool.field in self._fields
        ])