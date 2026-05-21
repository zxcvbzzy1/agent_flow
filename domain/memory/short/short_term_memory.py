from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Literal


memory_field = Literal[
    "tool_respond",
    "agent_history",
]


class ShortTermMemory(ABC):
    
    def __init__(self,fields:list[memory_field]):
        self.field = fields


    @abstractmethod
    async def store(self,fields:memory_field, key: str, raw: str) -> int:
        """存入工具原始输出，返回第几次调用（从 1 开始）。"""
        ...

    @abstractmethod
    def get(self,fields:memory_field, key: str, index: int) -> str | None:
        """取第 index 次原文，index=0 表示最后一次。"""
        ...


    @abstractmethod
    def all_keys(self) -> list[tuple[str, str]]: ...

    @abstractmethod
    def keys_by_field(self, field: memory_field) -> list[str]: ...

    @abstractmethod
    def count(self, field:memory_field,key: str) -> int: ...

    @abstractmethod
    def clear(self) -> None: ...

    @abstractmethod
    def clear_field(self, field: memory_field) -> None: ...
