# domain/memory/default_short_term_memory.py

import json
from domain.memory.short_term_memory import ShortTermMemory

MAX_LEN      = 700
RECENT_COUNT = 5


class DefaultShortTermMemory(ShortTermMemory):
    """
    默认实现：
    - 存：内存字典保存原文
    - 呈现：最近 N 条完整展示，较早的折叠
    - 摘要：超过 MAX_LEN 截断并提示可查询
    """

    def __init__(self):
        self._store:   dict[str, list[str]] = {}  # 原文
        self._summary: list[dict] = []            # 摘要列表，顺序与调用顺序一致

    # ── 存 ────────────────────────────────────────────────────────

    def store(self, tool_name: str, raw: str) -> int:
        if not isinstance(raw, str):
            raw = json.dumps(raw, ensure_ascii=False)

        self._store.setdefault(tool_name, []).append(raw)
        call_index = len(self._store[tool_name])

        self._summary.append({
            "tool_name": tool_name,
            "respond":   self._make_summary(tool_name, raw, call_index),
        })
        return call_index

    def _make_summary(self, tool_name: str, raw: str, call_index: int) -> str:
        if len(raw) > MAX_LEN:
            return (
                f"[{tool_name} 第{call_index}次] "
                f"{raw[:MAX_LEN]}... "
                f"(已截断，可用 query_tool_respond 查询完整内容)"
            )
        return f"[{tool_name} 第{call_index}次] {raw}"

    # ── 取 ────────────────────────────────────────────────────────

    def get(self, tool_name: str, index: int) -> str | None:
        history = self._store.get(tool_name, [])
        if not history:
            return None
        if index == 0:
            return history[-1]
        if 1 <= index <= len(history):
            return history[index - 1]
        return None

    # ── 呈现给 LLM ────────────────────────────────────────────────

    def to_prompt(self) -> str:
        if not self._summary:
            return ""
        parts = [f"- 工具反馈（共 {len(self._summary)} 条）："]
        for i, item in enumerate(self._summary):
            is_recent = i >= len(self._summary) - RECENT_COUNT
            if is_recent:
                parts.append(f"  [{item['tool_name']}] {item['respond']}")
            else:
                parts.append(
                    f"  [{item['tool_name']}] (已折叠，"
                    f"可调用 query_tool_respond(tool_name='{item['tool_name']}') 查看)"
                )
        return "\n".join(parts)

    # ── 其他 ──────────────────────────────────────────────────────

    def all_keys(self) -> list[str]:
        return list(self._store.keys())

    def count(self, tool_name: str) -> int:
        return len(self._store.get(tool_name, []))

    def clear(self) -> None:
        self._store.clear()
        self._summary.clear()