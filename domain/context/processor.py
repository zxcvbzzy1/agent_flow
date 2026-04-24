# domain/context/processor.py

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Awaitable

from domain.context.store.node import ContextNode

# 大纲生成函数类型：接收 (source_key, raw)，返回大纲字符串
OutlineFn = Callable[[str, str], Awaitable[str]]


class GranularityProcessor(ABC):

    @abstractmethod
    async def process(self, source_key: str, raw: str, scope: str) -> list[ContextNode]:
        ...



# ---------------------------------------------------------------------------
# 文档处理器
# ---------------------------------------------------------------------------

class DocumentProcessor(GranularityProcessor):
    """
    长文档拆分策略：
      skeleton —— LLM 生成的结构化大纲；默认 promoted
      chunk    —— 按 chunk_chars 字符切片；默认不 promoted
      full     —— 原始全文；默认不 promoted
    """

    def __init__(
        self,
        outline_fn:  OutlineFn | None = None,
        chunk_chars: int = 2000,
    ) -> None:
        self._outline_fn  = outline_fn
        self._chunk_chars = chunk_chars

    async def process(self, source_key: str, raw: str, scope: str) -> list[ContextNode]:
        nodes: list[ContextNode] = []
        chunk_count = max(1, len(raw) // self._chunk_chars)

        # ── skeleton：LLM 大纲 or 降级方案 ───────────────────────
        skeleton_text = await self._make_outline(source_key, raw)
        nodes.append(ContextNode(
            source_key=source_key,
            granularity="skeleton",
            content=skeleton_text,
            scope=scope,
            promoted=True,
        ))

        # ── chunks ────────────────────────────────────────────────
        for idx, start in enumerate(range(0, len(raw), self._chunk_chars)):
            nodes.append(ContextNode(
                source_key=source_key,
                granularity="chunk",
                content=raw[start: start + self._chunk_chars],
                chunk_index=idx,
                scope=scope,
                promoted=False,
            ))

        # ── full ──────────────────────────────────────────────────
        nodes.append(ContextNode(
            source_key=source_key,
            granularity="full",
            content=raw,
            scope=scope,
            promoted=False,
        ))

        return nodes

    async def _make_outline(self, source_key: str, raw: str) -> str:
        if self._outline_fn is None:
            # 降级：提取标题行或前 10 行
            heading_lines = [l for l in raw.splitlines() if l.startswith("#")]
            fallback = (
                "\n".join(heading_lines)
                if heading_lines
                else "\n".join(raw.splitlines()[:10])
            )
            return fallback 

        try:
            outline = await self._outline_fn(source_key, raw)
            return outline 
        except Exception as e:
            heading_lines = [l for l in raw.splitlines() if l.startswith("#")]
            fallback = (
                "\n".join(heading_lines)
                if heading_lines
                else "\n".join(raw.splitlines()[:10])
            )
            print(f"[DocumentProcessor] outline_fn failed for '{source_key}': {e}")
            return fallback


# ---------------------------------------------------------------------------
# 工具输出处理器
# ---------------------------------------------------------------------------

class ToolOutputProcessor(GranularityProcessor):
    """
    工具输出拆分策略：
      短输出（≤ short_threshold）：只生成一个 full 节点，直接 promoted。
      长输出：
        skeleton —— LLM 生成的大纲摘要；promoted
        chunk    —— 按 chunk_chars 切片；不 promoted
    """

    def __init__(
        self,
        outline_fn:      OutlineFn | None = None,
        short_threshold: int = 800,
        chunk_chars:     int = 2000,
    ) -> None:
        self._outline_fn  = outline_fn
        self._short       = short_threshold
        self._chunk_chars = chunk_chars

    async def process(self, source_key: str, raw: str, scope: str) -> list[ContextNode]:
        if len(raw) <= self._short:
            return [ContextNode(
                source_key=source_key,
                granularity="full",
                content=raw,
                scope=scope,
                promoted=True,
            )]

        nodes: list[ContextNode] = []
        chunk_count = max(1, len(raw) // self._chunk_chars)

        # ── skeleton ──────────────────────────────────────────────
        skeleton_text = await self._make_outline(source_key, raw)
        nodes.append(ContextNode(
            source_key=source_key,
            granularity="skeleton",
            content=skeleton_text,
            scope=scope,
            promoted=True,
        ))

        # ── chunks ────────────────────────────────────────────────
        for idx, start in enumerate(range(0, len(raw), self._chunk_chars)):
            nodes.append(ContextNode(
                source_key=source_key,
                granularity="chunk",
                content=raw[start: start + self._chunk_chars],
                chunk_index=idx,
                scope=scope,
                promoted=False,
            ))

        return nodes

    async def _make_outline(self, source_key: str, raw: str) -> str:
        if self._outline_fn is None:
            return raw[:self._short]
        try:
            outline = await self._outline_fn(source_key, raw)
            return outline
        except Exception as e:
            print(f"[ToolOutputProcessor] outline_fn failed for '{source_key}': {e}")
            return raw[:self._short] 


# ---------------------------------------------------------------------------
# 对话历史处理器
# ---------------------------------------------------------------------------

class HistoryProcessor(GranularityProcessor):
    """每轮对话作为一个 chunk 追加，初始 promoted=True。"""

    async def process(self, source_key: str, raw: str, scope: str) -> list[ContextNode]:
        return [ContextNode(
            source_key=source_key,
            granularity="chunk",
            content=raw,
            chunk_index=0,
            scope=scope,
            promoted=True,
        )]