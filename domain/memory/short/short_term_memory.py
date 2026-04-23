# domain/memory/short_term_memory.py

from abc import ABC, abstractmethod


class ShortTermMemory(ABC):
    """
    短期记忆模块接口。

    职责：
    - store()        接收工具原始输出，决定怎么存
    - to_prompt()    决定怎么呈现给 LLM（摘要/折叠/完整）
    - get()          按工具名和次数取原文，供 $ref 引用
    - 轮次缓存      管理本轮工具执行结果，供 $ref#0 引用
    """

    @abstractmethod
    async def store(self, tool_name: str, raw: str,callBack) -> int:
        """存入工具原始输出，返回第几次调用（从1开始）。"""
        ...

    @abstractmethod
    def get(self, tool_name: str, index: int) -> str | None:
        """取第 index 次原文，index=0 表示最后一次。"""
        ...

    @abstractmethod
    def to_prompt(self) -> str:
        """生成决策层 prompt 片段，由 _build_state_summary 调用。"""
        ...

    @abstractmethod
    def all_keys(self) -> list[str]:
        ...

    @abstractmethod
    def count(self, tool_name: str) -> int:
        ...

    @abstractmethod
    def clear(self) -> None:
        ...

    # ── 轮次缓存 ────────────────────────────────────────────────

    @abstractmethod
    def begin_round(self) -> None:
        """每轮工具执行开始前调用，清空本轮缓存。"""
        ...

    @abstractmethod
    def store_round(self, tool_name: str, raw: str) -> None:
        """存入本轮结果，供同轮 $ref#0 引用。"""
        ...

    @abstractmethod
    def get_round(self, tool_name: str) -> str | None:
        """取本轮某工具的缓存结果。"""
        ...
