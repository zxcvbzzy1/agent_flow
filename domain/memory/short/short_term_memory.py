# domain/memory/short_term_memory.py

from abc import ABC, abstractmethod
import re


class ShortTermMemory(ABC):
    """
    短期记忆模块接口。
    
    职责：
    - store()        接收工具原始输出，决定怎么存
    - to_prompt()    决定怎么呈现给 LLM（摘要/折叠/完整）
    - get()          按工具名和次数取原文，供 $ref 引用
    - resolve_ref()  解析 $ref 占位符
    """

    @abstractmethod
    def store(self, tool_name: str, raw: str) -> int:
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

    # ── 公共逻辑，子类无需重写 ────────────────────────────────────

    def resolve_ref(self, ref: str, round_results: dict[str, str]) -> str:
        m = re.fullmatch(r"\$ref:([^#]+)#(\d+)", ref)
        if not m:
            return ref
        tool_name, index = m.group(1), int(m.group(2))
        if index == 0 and tool_name in round_results:
            return round_results[tool_name]
        result = self.get(tool_name, index)
        if result is None:
            print(f"[WARN] $ref:{tool_name}#{index} 未找到，"
                  f"共调用 {self.count(tool_name)} 次，"
                  f"已存工具：{self.all_keys()}")
            return ""
        return result

    def is_ref(self, value: str) -> bool:
        return isinstance(value, str) and bool(
            re.fullmatch(r"\$ref:[^#]+#\d+", value)
        )