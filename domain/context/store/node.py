"""
domain/context/node.py

上下文存储的最小单元。
一份资源（一个工具输出、一个文档、一轮对话）可以同时存在多个粒度的 node。
节点本身不携带任何处理策略，只描述"这段内容是什么、处于哪个粒度"。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal

# 粒度层级：
#   skeleton  —— 结构摘要/标题/前几行，默认注入，token 极少
#   chunk     —— 按段切分的片段，按需 promote
#   full      —— 原始全文，极少直接注入
Granularity = Literal["skeleton", "chunk", "full"]


@dataclass
class ContextNode:
    """
    一个资源在某个粒度下的表示。

    字段说明
    --------
    source_key  : 资源唯一标识，如 "report.txt"、"tool:read_file#2"
    granularity : 粒度层级
    content     : 该粒度下的实际文本内容
    chunk_index : 仅 granularity=="chunk" 时有意义，从 0 开始
    tokens      : 粗估 token 数（len(content)//4），由 __post_init__ 自动填充
    scope       : 所属 scope，与 SlotScope 对应（memory / history / task …）
    promoted    : True 表示当前在注入窗口中，False 表示已 demote 或尚未激活
    created_at  : 写入时间戳，用于 demote 时的 LRU 排序
    metadata    : 扩展字段，不影响核心逻辑
    """

    source_key:  str
    granularity: Granularity
    content:     str
    chunk_index: int   = 0
    tokens:      int   = 0
    scope:       str   = "memory"
    promoted:    bool  = False
    created_at:  float = field(default_factory=time.time)
    metadata:    dict  = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.tokens:
            self.tokens = len(self.content) // 4

    # ── 便捷谓词 ──────────────────────────────────────────────────

    def is_skeleton(self) -> bool:
        return self.granularity == "skeleton"

    def is_chunk(self) -> bool:
        return self.granularity == "chunk"

    def is_full(self) -> bool:
        return self.granularity == "full"

    def label(self) -> str:
        """供 Provider 格式化时使用的人类可读标签"""
        if self.granularity == "skeleton":
            return f"{self.source_key}  [结构]"
        if self.granularity == "chunk":
            return f"{self.source_key}  [片段 {self.chunk_index}]"
        return self.source_key