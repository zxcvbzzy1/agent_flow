"""
domain/memory/default_short_term_memory.py

ShortTermMemory 的默认实现。
保存工具原始输出的完整原文，不做任何截断或压缩。
"""
from __future__ import annotations

import json
from typing import Callable, Awaitable

from domain.memory.short.short_term_memory import ShortTermMemory


class DefaultShortTermMemory(ShortTermMemory):
    """
    存储结构
    --------
    _store        : {tool_name: [raw_str, ...]}  原始输出列表
    _summary      : [{"tool_name", "summary", "index"}, ...]  摘要列表
    _round_cache  : {tool_name: raw_str}  本轮工具输出缓存（跨步骤引用用）

    """

    def __init__(self) -> None:
        self._store:       dict[str, list[str]] = {}
        self._summary:     list[dict]           = []
        self._round_cache: dict[str, str]       = {}


    # ── 写入 ──────────────────────────────────────────────────────

    def store(self, tool_name: str, raw: str) -> int:
        if not isinstance(raw, str):
            raw = json.dumps(raw, ensure_ascii=False)

        self.store_round(tool_name, raw)
        self._store.setdefault(tool_name, []).append(raw)
        # call_index = len(self._store[tool_name])
        # summary = raw
        # self._summary.append({
        #     "tool_name": tool_name,
        #     "summary":   summary,
        #     "index":     call_index,
        # })
        # return call_index


    # ── 读取 ──────────────────────────────────────────────────────

    def get(self, tool_name: str, index: int) -> str | None:
        history = self._store.get(tool_name, [])
        if not history:
            return None
        if index == 0:
            return history[-1]
        if 1 <= index <= len(history):
            return history[index - 1]
        return None

    def get_summary_list(self) -> list[dict]:
        return list(self._summary)

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