

import os
from typing import Collection

from domain.context.strategy import ContextItem, ItemStrategy
from infra.db.milvus_rag import MilvusRAG


class ChunkToFileStrategy(ItemStrategy):
    """
    超出 token_limit 的 item 按 chunk_tokens 切块：
      - 每块写入文件 {storage_dir}/{source}_{i}.txt
      - 同时调用 rag.insert() 向量化存入 Milvus
      - 原 item 替换为占位 ContextItem，标记 offloaded=True
    """

    def __init__(
        self,
        storage_dir: str,
        token_limit: int = 4000,
        chunk_tokens: int = 4000,
        rag:MilvusRAG = None,
        col:Collection = None,
    ) -> None:
        self._rag         = rag
        self._col         = col
        self._storage_dir = storage_dir
        self._token_limit = token_limit
        self._chunk_size  = chunk_tokens 
        os.makedirs(storage_dir, exist_ok=True)

    def transform(self, items: list[ContextItem], state: dict) -> list[ContextItem]:
        result: list[ContextItem] = []
        for item in items:
            if item.metadata["tool_name"] == "read_files":
                result.append(item)
                continue
            if item.tokens <= self._token_limit:
                result.append(item)
                continue

            chunks = self._split(item.content)
            paths = []
            for idx, chunk in enumerate(chunks):
                paths.append(self._save_to_file(item.source, idx, chunk))
                if self._rag is None:
                    continue
                self._rag.insert(self._col, item.source, idx, chunk)

            paths_text = "\n".join(paths)
            result.append(ContextItem(
                source=item.source,
                content=f"[内容已卸载至文件，共 {len(chunks)} 块，路径如下所示：\n{paths_text}\n,可按需查询]",
                metadata={**item.metadata, "offloaded": True, "chunk_count": len(chunks)},
            ))
        return result

    def _split(self, text: str) -> list[str]:
        return [
            text[i: i + self._chunk_size]
            for i in range(0, len(text), self._chunk_size)
        ]

    def _save_to_file(self, source: str, idx: int, content: str) -> str:
        safe_name = source.replace(":", "_").replace("/", "_")
        path = os.path.join(self._storage_dir, f"{safe_name}_{idx}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path




class RAGRecallStrategy(ItemStrategy):
    """
    用 state["prompt"] 从 Milvus 召回相关块，
    注入为新的 ContextItem 追加到列表头部。
    """

    def __init__(self, rag: MilvusRAG, col: Collection) -> None:
        self._rag = rag
        self._col = col

    def transform(self, items: list[ContextItem], state: dict) -> list[ContextItem]:
        query = state.get("prompt", "").strip()
        if not query:
            return items

        hits = self._rag.search(self._col, query)
        recalled = [
            ContextItem(
                source=f"[RAG] {h['source']}#{h['chunk_index']}",
                content=h["content"],
                metadata={
                    "recalled": True,
                    "score":    h["score"],
                    "origin":   h["source"],
                },
            )
            for h in hits
        ]
        return recalled + items