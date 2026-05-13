from __future__ import annotations
import hashlib
from typing import Callable
from pymilvus import (
    MilvusClient, Collection, DataType,
    AnnSearchRequest, WeightedRanker,
)


class MilvusRAG:

    _instance = None
    _instance_bool = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(
        self,
        embed_fn:   Callable[[str], list[float]],
        sparse_embed_fn: Callable[[str], dict[int, float]],
        uri:       str = "http://localhost:19530",
        top_k:      int = 3,
        score_threshold: float = 0.75,
    ) -> None:
        if self.__class__._instance_bool:
            return
        self._embed_fn         = embed_fn
        self._sparse_embed_fn  = sparse_embed_fn
        self._top_k            = top_k
        self._score_threshold  = score_threshold
        self.uri = uri

        self.client = None
        self.__class__._instance_bool = True


    def connect(self,):
        self.client = MilvusClient(
            uri=self.uri,
            token="root:Milvus"
        )
        return self.client

    def use_db(self,db_name):
        if db_name in self.client.list_databases():
            self.client.use_database(
                db_name=db_name
            )
        else:
            self.client.create_database(
                db_name=db_name
            )
            self.client.use_database(
                db_name=db_name
            )
            
    def _get_or_create_collection(
        self,
        collection_name: str,
        dense_dim: int,
    ) -> Collection:
        if not self.client.has_collection(collection_name):
            schema = self.client.create_schema(auto_id=False, enable_dynamic_field=False)
            schema.add_field("chunk_id",    DataType.VARCHAR,      max_length=32,   is_primary=True)
            schema.add_field("source",      DataType.VARCHAR,      max_length=256)
            schema.add_field("chunk_index", DataType.INT64)
            schema.add_field("content",     DataType.VARCHAR,      max_length=8192)
            schema.add_field("dense_vector",  DataType.FLOAT_VECTOR, dim=dense_dim)
            schema.add_field("sparse_vector", DataType.SPARSE_FLOAT_VECTOR)

            index_params = self.client.prepare_index_params()
            index_params.add_index(
                field_name="dense_vector",
                index_type="IVF_FLAT",
                metric_type="COSINE",
                params={"nlist": 128},
            )
            index_params.add_index(
                field_name="sparse_vector",
                index_type="SPARSE_INVERTED_INDEX",
                metric_type="IP",
            )

            self.client.create_collection(
                collection_name=collection_name,
                schema=schema,
                index_params=index_params,
            )

        col = Collection(collection_name)
        col.load()
        return col
    
    def release_collection(self,name)->bool:
        self.client.release_collection(
            collection_name=name
        )
        res = self.client.get_load_state(
            collection_name=name
        )
        if res["state"].value == 1:
            return True
        else:
            return False


    # ── 写 ────────────────────────────────────────────────────────

    def insert(self, col: Collection, source: str, chunk_index: int, content: str) -> None:
        """将一个文本块向量化后存入 Milvus。"""
        chunk_id = self._make_id(source, chunk_index)
        dense_vector  = self._embed_fn(content)
        sparse_vector = self._sparse_embed_fn(content)
        col.insert([
            [chunk_id],      # chunk_id       (VARCHAR)
            [source],        # source         (VARCHAR)
            [chunk_index],   # chunk_index    (INT64)
            [content],       # content        (VARCHAR)
            [dense_vector],  # dense_vector   (FLOAT_VECTOR)
            [sparse_vector], # sparse_vector  (SPARSE_FLOAT_VECTOR)
        ])
        col.flush()

    # ── 查 ────────────────────────────────────────────────────────

    def search(self, col: Collection, query: str) -> list[dict]:
        """
        混合检索（dense + sparse），返回 [{"source", "chunk_index", "content", "score"}, ...]
        score 越高越相关。
        """
        dense_vector  = self._embed_fn(query)
        sparse_vector = self._sparse_embed_fn(query)

        dense_req = AnnSearchRequest(
            data=[dense_vector],
            anns_field="dense_vector",
            param={"metric_type": "COSINE", "params": {"nprobe": 10}},
            limit=self._top_k,
        )
        sparse_req = AnnSearchRequest(
            data=[sparse_vector],
            anns_field="sparse_vector",
            param={"metric_type": "IP"},
            limit=self._top_k,
        )

        results = col.hybrid_search(
            requests=[dense_req, sparse_req],
            ranker=WeightedRanker(0.7, 0.3),
            limit=self._top_k,
            output_fields=["source", "chunk_index", "content"],
        )

        hits = []
        for hit in results[0]:
            score = hit.score
            if score < self._score_threshold:
                continue
            hits.append({
                "source":      hit.entity.get("source"),
                "chunk_index": hit.entity.get("chunk_index"),
                "content":     hit.entity.get("content"),
                "score":       score,
            })
        return hits

    # ── 内部 ─────────────────────────────────────────────────────

    def _make_id(self, source: str, chunk_index: int) -> str:
        return hashlib.md5(f"{source}#{chunk_index}".encode()).hexdigest()[:16]