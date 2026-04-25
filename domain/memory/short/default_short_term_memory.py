# domain/memory/default_short_term_memory.py
from __future__ import annotations
import json
from domain.memory.short.short_term_memory import ShortTermMemory,memory_field


class DefaultShortTermMemory(ShortTermMemory):
    """
    存储结构
    --------
    _store       : {tool_name: [raw_str, ...]}   原始输出列表，按调用顺序追加
    _round_cache : {tool_name: raw_str}           本轮工具输出缓存，供跨步骤 $ref 引用
    """

    def __init__(self,fields:memory_field) -> None:
        super.__init__(fields)
        self._store:       dict[str, list[str]] = {}


    # ── 写 ────────────────────────────────────────────────────────

    def store(self,field, key: str, raw: str) -> None:
        if not isinstance(raw, str):
            raw = json.dumps(raw, ensure_ascii=False)
        self._store.setdefault(field, {}).setdefault(key, []).append(raw)

    # ── 读 ────────────────────────────────────────────────────────

    def get(self,field, key: str, index: int = 0) -> str | None:
        history = self._store.get(field, {})
        if not history:
            return None
        else:
            key_history = history.get(key,[])
            if not key_history:
                return None
            if index == 0:
                return key_history[-1]
            if 1 <= index <= len(key_history):
                return key_history[index - 1]
            return None

    def count(self,field, key: str) -> int:
        mid_field = self._store.get(field,None)
        if mid_field is None:
            return 0
        else:
            return len(mid_field.get(key, []))

    def all_keys(self) -> list[str]:
        return list(self._store.keys())


    # ── 清空 ──────────────────────────────────────────────────────

    def clear(self) -> None:
        self._store.clear()