# domain/memory/default_short_term_memory.py

import json
from typing import Callable, Awaitable
from domain.memory.short.short_term_memory import ShortTermMemory

MAX_LEN      = 700
RECENT_COUNT = 5

# summarize_fn: 接收 (tool_name, raw, call_index)，返回摘要字符串
SummarizeFn = Callable[[str, str, int], Awaitable[str]]


class DefaultShortTermMemory(ShortTermMemory):
    """
    默认实现：
    - 存：内存字典保存原文
    - 呈现：最近 N 条完整展示，较早的折叠
    - 摘要：通过注入的 summarize_fn（LLM 总结），短文本直接格式化
    """

    def __init__(self, summarize_fn: SummarizeFn | None = None):
        self._store:       dict[str, list[str]] = {}  # 原文
        self._summary:     list[dict] = []            # 摘要列表，顺序与调用顺序一致
        self._round_cache: dict[str, str] = {}        # 本轮缓存
        self._summarize_fn = summarize_fn              # LLM 总结函数（由 infra 层注入）

    # ── 存 ────────────────────────────────────────────────────────

    async def store(self, tool_name: str, raw: str, callBack) -> int:
        if not isinstance(raw, str):
            raw = json.dumps(raw, ensure_ascii=False)
        self.store_round(tool_name, raw)
        self._store.setdefault(tool_name, []).append(raw)
        call_index = len(self._store[tool_name])
        if callBack is None:
            summary = await self._make_summary(tool_name, raw, call_index)
            self._summary.append({
                "tool_name": tool_name,
                "respond":   summary,
            })
        else:
            self._summary.append({
                "tool_name": tool_name,
                "respond":   callBack(),
            })
        return call_index

    async def _make_summary(self, tool_name: str, raw: str, call_index: int) -> str:
        if len(raw) > MAX_LEN and self._summarize_fn is not None:
            llm_summary = await self._summarize_fn(tool_name, raw, call_index)
            return f"[{tool_name} 第{call_index}次] {llm_summary} (可用 query_tool_respond 查询完整内容)"
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

    # ── 轮次缓存 ──────────────────────────────────────────────────

    def begin_round(self) -> None:
        self._round_cache.clear()

    def store_round(self, tool_name: str, raw: str) -> None:
        self._round_cache[tool_name] = raw

    def get_round(self, tool_name: str) -> str | None:
        return self._round_cache.get(tool_name)

    # ── 其他 ──────────────────────────────────────────────────────

    def all_keys(self) -> list[str]:
        return list(self._store.keys())

    def count(self, tool_name: str) -> int:
        return len(self._store.get(tool_name, []))

    def clear(self) -> None:
        self._store.clear()
        self._summary.clear()
        self._round_cache.clear()