"""
ContextStore：上下文窗口的管理者。

动态调度加载进prompt的上下文

职责
----
- write()         原始内容通过 Processor 拆粒度后存入节点池
- explore()       Agent 主动 promote 指定节点进入注入窗口
- window()        返回当前所有 promoted 节点（Provider 的数据来源）
- _enforce_budget 超出 token_limit 时按优先级 demote 节点，写入文件持久化

不做的事
--------
- 不格式化文字（那是 Provider 的事）
- 不压缩内容（调用方在 write 之前处理好内容，Store 只存结果）
- 不知道 ContextEngine 和 Provider 的存在
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Awaitable

from domain.context.store.node import ContextNode, Granularity
from domain.context.processor import GranularityProcessor


class ContextStore:
    """
    Parameters
    ----------
    token_limit  : promoted 节点的 token 总量上限，超出时触发 demote
    storage_dir  : demoted 节点持久化到此目录，None 则不写文件
    """

    # demote 优先级：full 最先被踢出，skeleton 最后
    _DEMOTE_ORDER: list[Granularity] = ["full", "chunk", "skeleton"]

    def __init__(
        self,
        token_limit: int = 100000,
        storage_dir: str | None = None,
    ) -> None:
        self._nodes:      list[ContextNode]              = []
        self._processors: dict[str, GranularityProcessor] = {}
        self._token_limit = token_limit
        self._storage_dir = Path(storage_dir) if storage_dir else None

        if self._storage_dir:
            self._storage_dir.mkdir(parents=True, exist_ok=True)

    # ── Processor 注册 ────────────────────────────────────────────

    def register_processor(
        self, scope: str, processor: GranularityProcessor
    ) -> None:
        """
        为某个 scope 注册粒度处理器。
        同一 scope 只能有一个 processor，后注册的覆盖前者。

        示例
        ----
        store.register_processor("memory",  ToolOutputProcessor())
        store.register_processor("document", DocumentProcessor())
        store.register_processor("history", HistoryProcessor())
        """
        self._processors[scope] = processor

    # ── Write ─────────────────────────────────────────────────────

    async def write(
        self,
        source_key: str,
        raw:        str,
        scope:      str,
        metadata:   dict | None = None,
    ) -> list[ContextNode]:
        """
        写入一份原始内容。

        1. 如果 scope 有注册的 Processor，交给它拆粒度。
        2. 否则整体作为一个 full 节点，直接 promoted。
        3. 同一 source_key 的旧节点先全部移除（覆盖写）。
        4. 写入后触发 token 预算检查。

        Returns
        -------
        写入的节点列表（已存入 store）
        """
        processor = self._processors.get(scope)
        if processor:
            nodes = await processor.process(source_key, raw, scope)
        else:
            nodes = [ContextNode(
                source_key=source_key,
                granularity="full",
                content=raw,
                scope=scope,
                promoted=True,
            )]

        if metadata:
            for n in nodes:
                n.metadata.update(metadata)

        # 覆盖旧节点
        self._nodes = [n for n in self._nodes if n.source_key != source_key]
        self._nodes.extend(nodes)

        self._enforce_budget()
        return nodes

    # ── Explore ───────────────────────────────────────────────────

    def explore(
        self,
        source_key:  str,
        granularity: Granularity = "chunk",
        chunk_index: int = 0,
    ) -> ContextNode | None:
        """
        主动按需 promote 一个节点进入注入窗口。

        Returns
        -------
        promoted 的节点，或 None（节点不存在时）。
        调用方可以在 None 时去外部加载内容再 write()。
        """
        node = self._find(source_key, granularity, chunk_index)
        if node is None:
            return None
        node.promoted = True
        self._enforce_budget()
        return node

    def explore_all_chunks(self, source_key: str) -> list[ContextNode]:
        """promote 某资源的所有 chunk 节点"""
        targets = [
            n for n in self._nodes
            if n.source_key == source_key and n.granularity == "chunk"
        ]
        for n in targets:
            n.promoted = True
        self._enforce_budget()
        return targets

    def demote(self, source_key: str, granularity: Granularity, chunk_index: int = 0) -> None:
        """手动将某个节点移出注入窗口（不删除，只改 promoted 状态）"""
        node = self._find(source_key, granularity, chunk_index)
        if node:
            node.promoted = False

    # ── Window（Provider 的数据来源）─────────────────────────────

    def window(self, scope: str | None = None) -> list[ContextNode]:
        """
        返回当前所有 promoted 节点，按写入顺序排列。
        Provider 调用此方法获取要格式化的内容。
        """
        return [
            n for n in self._nodes
            if n.promoted and (scope is None or n.scope == scope)
        ]


    # ── Token 预算管理 ────────────────────────────────────────────

    def _enforce_budget(self) -> None:
        """
        超出 token_limit 时按粒度优先级 demote 节点：
          full → chunk → skeleton（skeleton 最后动）
        同粒度内按 created_at 升序（最旧的先 demote）。
        demoted 节点写入文件持久化。
        """
        if self.promoted_token_count() <= self._token_limit:
            return

        for granularity in self._DEMOTE_ORDER:
            candidates = sorted(
                [n for n in self._nodes
                 if n.promoted and n.granularity == granularity],
                key=lambda n: n.created_at,
            )
            for node in candidates:
                # self._write_to_file(node)
                node.promoted = False
                if self.promoted_token_count() <= self._token_limit:
                    return

    # ── 文件持久化 ────────────────────────────────────────────────

    def _write_to_file(self, node: ContextNode) -> None:
        """把 node 追加写入 {storage_dir}/{scope}.jsonl"""
        if not self._storage_dir:
            return
        path = self._storage_dir / f"{node.scope}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "source_key":  node.source_key,
                "granularity": node.granularity,
                "chunk_index": node.chunk_index,
                "content":     node.content,
                "scope":       node.scope,
                "tokens":      node.tokens,
                "created_at":  node.created_at,
                "metadata":    node.metadata,
            }, ensure_ascii=False) + "\n")

    def load_from_file(self, scope: str) -> list[ContextNode]:
        """
        从文件加载某个 scope 的历史节点（demoted 的冷存储）。
        加载后 promoted=False，调用方可按需 promote。
        """
        if not self._storage_dir:
            return []
        path = self._storage_dir / f"{scope}.jsonl"
        if not path.exists():
            return []

        loaded: list[ContextNode] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            d = json.loads(line)
            node = ContextNode(
                source_key=d["source_key"],
                granularity=d["granularity"],
                content=d["content"],
                chunk_index=d.get("chunk_index", 0),
                scope=d.get("scope", scope),
                tokens=d.get("tokens", 0),
                created_at=d.get("created_at", 0.0),
                metadata=d.get("metadata", {}),
                promoted=False,
            )
            loaded.append(node)
        return loaded

    # ── 内部查找 ──────────────────────────────────────────────────

    def _find(
        self,
        source_key:  str,
        granularity: Granularity,
        chunk_index: int = 0,
    ) -> ContextNode | None:
        for n in self._nodes:
            if n.source_key != source_key:
                continue
            if n.granularity != granularity:
                continue
            if granularity == "chunk" and n.chunk_index != chunk_index:
                continue
            return n
        return None
    
    # ── 查询辅助 ──────────────────────────────────────────────────

    def skeleton_of(self, source_key: str) -> ContextNode | None:
        return self._find(source_key, "skeleton")

    def chunks_of(self, source_key: str) -> list[ContextNode]:
        return sorted(
            [n for n in self._nodes
             if n.source_key == source_key and n.granularity == "chunk"],
            key=lambda n: n.chunk_index,
        )

    def all_source_keys(self) -> list[str]:
        seen: dict[str, None] = {}
        for n in self._nodes:
            seen[n.source_key] = None
        return list(seen)

    def promoted_token_count(self) -> int:
        return sum(n.tokens for n in self._nodes if n.promoted)
