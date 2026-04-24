"""
ShortTermMemory 抽象接口。
负责存储工具的原始输出，供 AgentBase.on_tool_call() 写入，
以及 ContextStore 通过 write() 消费。

与 ContextStore 的分工
----------------------
ShortTermMemory  ——  存工具原始输出（原文，不截断）
ContextStore     ——  管理哪些内容进入 LLM 上下文窗口（promoted window）
"""
from abc import ABC, abstractmethod


class ShortTermMemory(ABC):

    @abstractmethod
    async def store(self, tool_name: str, raw: str, callback=None) -> int:
        """存入工具原始输出，返回第几次调用（从 1 开始）。"""
        ...

    @abstractmethod
    def get(self, tool_name: str, index: int) -> str | None:
        """取第 index 次原文，index=0 表示最后一次。"""
        ...

    @abstractmethod
    def get_summary_list(self) -> list[dict]:
        """
        返回所有摘要条目，供需要时使用。
        每条格式：{"tool_name": str, "summary": str, "index": int}
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