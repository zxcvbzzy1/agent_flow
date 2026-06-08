from __future__ import annotations

import copy
import time
from typing import Any

try:
    from pymongo import MongoClient
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except Exception:  # pragma: no cover - pymongo is optional at import time
    MongoClient = None
    PyMongoError = Exception
    ServerSelectionTimeoutError = Exception


class DocumentStore:
    """Small MongoDB wrapper with an in-memory fallback for local tests/dev."""

    def __init__(self, mongo_url: str, db_name: str) -> None:
        self._memory: dict[str, list[dict[str, Any]]] = {}
        self._db = None
        self.using_memory = True

        if MongoClient is None:
            return

        try:
            client = MongoClient(mongo_url, serverSelectionTimeoutMS=300)
            client.admin.command("ping")
            self._db = client[db_name]
            self.using_memory = False
        except (PyMongoError, ServerSelectionTimeoutError, OSError):
            self._db = None
            self.using_memory = True

    def insert_one(self, collection: str, document: dict[str, Any]) -> dict[str, Any]:
        doc = self._stamp(copy.deepcopy(document), create=True)
        if self._db is not None:
            self._db[collection].insert_one(copy.deepcopy(doc))
        else:
            self._memory.setdefault(collection, []).append(copy.deepcopy(doc))
        return copy.deepcopy(doc)

    def find_one(self, collection: str, query: dict[str, Any]) -> dict[str, Any] | None:
        if self._db is not None:
            doc = self._db[collection].find_one(query, {"_id": False})
            return copy.deepcopy(doc) if doc else None

        for doc in self._memory.get(collection, []):
            if self._matches(doc, query):
                return copy.deepcopy(doc)
        return None

    def find_many(
        self,
        collection: str,
        query: dict[str, Any] | None = None,
        sort: list[tuple[str, int]] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        query = query or {}
        if self._db is not None:
            cursor = self._db[collection].find(query, {"_id": False})
            if sort:
                cursor = cursor.sort(sort)
            if limit:
                cursor = cursor.limit(limit)
            return [copy.deepcopy(doc) for doc in cursor]

        docs = [
            copy.deepcopy(doc)
            for doc in self._memory.get(collection, [])
            if self._matches(doc, query)
        ]
        for key, direction in reversed(sort or []):
            docs.sort(key=lambda item: item.get(key, 0), reverse=direction < 0)
        return docs[:limit] if limit else docs

    def ensure_index(self, collection: str, keys: list[tuple[str, int]], **kwargs: Any) -> None:
        """在 Mongo 上建索引（幂等，已存在则忽略）；内存兜底为 no-op。

        懒加载窗口查询按 (conversation_id/room_id 等值) + (created_at 范围/排序) 命中，
        建好对应复合索引后只触达窗口内文档，避免整会话扫描，真正降低 DB 压力。
        """
        if self._db is None:
            return
        try:
            self._db[collection].create_index(keys, **kwargs)
        except PyMongoError:
            pass

    def update_one(
        self,
        collection: str,
        query: dict[str, Any],
        updates: dict[str, Any],
        upsert: bool = False,
    ) -> dict[str, Any] | None:
        updates = self._stamp(copy.deepcopy(updates), create=False)
        if self._db is not None:
            self._db[collection].update_one(query, {"$set": updates}, upsert=upsert)
            return self.find_one(collection, query)

        docs = self._memory.setdefault(collection, [])
        for doc in docs:
            if self._matches(doc, query):
                doc.update(copy.deepcopy(updates))
                return copy.deepcopy(doc)
        if upsert:
            doc = {**query, **updates}
            docs.append(copy.deepcopy(doc))
            return copy.deepcopy(doc)
        return None

    def delete_one(self, collection: str, query: dict[str, Any]) -> int:
        if self._db is not None:
            result = self._db[collection].delete_one(query)
            return int(result.deleted_count)

        docs = self._memory.setdefault(collection, [])
        for index, doc in enumerate(docs):
            if self._matches(doc, query):
                del docs[index]
                return 1
        return 0

    def delete_many(self, collection: str, query: dict[str, Any]) -> int:
        if self._db is not None:
            result = self._db[collection].delete_many(query)
            return int(result.deleted_count)

        docs = self._memory.setdefault(collection, [])
        before = len(docs)
        self._memory[collection] = [
            doc for doc in docs
            if not self._matches(doc, query)
        ]
        return before - len(self._memory[collection])

    def clear(self) -> None:
        if self._db is not None:
            for name in self._db.list_collection_names():
                self._db[name].delete_many({})
        self._memory.clear()

    def _stamp(self, document: dict[str, Any], create: bool) -> dict[str, Any]:
        now = time.time()
        if create:
            document.setdefault("created_at", now)
        document["updated_at"] = now
        return document

    def _matches(self, document: dict[str, Any], query: dict[str, Any]) -> bool:
        return all(self._match_field(document.get(key), condition) for key, condition in query.items())

    def _match_field(self, value: Any, condition: Any) -> bool:
        # 仅当 condition 是非空、且键全部以 $ 开头的字典时按操作符处理；
        # 否则按普通相等匹配（含对普通 dict 值的整体相等比较，保持原行为）。
        if isinstance(condition, dict) and condition and all(str(k).startswith("$") for k in condition):
            for op, operand in condition.items():
                if op == "$lt":
                    if value is None or not value < operand:
                        return False
                elif op == "$lte":
                    if value is None or not value <= operand:
                        return False
                elif op == "$gt":
                    if value is None or not value > operand:
                        return False
                elif op == "$gte":
                    if value is None or not value >= operand:
                        return False
                elif op == "$ne":
                    if value == operand:
                        return False
                elif op == "$in":
                    if value not in (operand or []):
                        return False
                elif op == "$nin":
                    if value in (operand or []):
                        return False
                elif op == "$exists":
                    if bool(operand) != (value is not None):
                        return False
                else:
                    # 未支持的操作符：保守地判为不匹配，避免静默误命中。
                    return False
            return True
        return value == condition
