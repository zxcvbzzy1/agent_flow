# domain/memory/short/short_term_memory.py

from abc import ABC, abstractmethod


class ShortTermMemory(ABC):

    @abstractmethod
    async def store(self, tool_name: str, raw: str, callback) -> int:
        """存入工具原始输出，返回第几次调用（从1开始）。"""
        ...

    @abstractmethod
    def get(self, tool_name: str, index: int) -> str | None:
        """取第 index 次原文，index=0 表示最后一次。"""
        ...

    @abstractmethod
    def get_summary_list(self) -> list[dict]:
        """
        返回所有摘要条目，供 ContextProvider 格式化。
        每条格式：{"tool_name": str, "summary": str, "index": int}
        不包含呈现逻辑，只暴露数据。
        """
        ...

    @abstractmethod
    def all_keys(self) -> list[str]: ...

    @abstractmethod
    def count(self, tool_name: str) -> int: ...

    @abstractmethod
    def clear(self) -> None: ...

    @abstractmethod
    def begin_round(self) -> None: ...

    @abstractmethod
    def store_round(self, tool_name: str, raw: str) -> None: ...

    @abstractmethod
    def get_round(self, tool_name: str) -> str | None: ...
