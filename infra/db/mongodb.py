from __future__ import annotations

import copy
import time
from typing import Any

from pymongo import MongoClient, ReturnDocument
from pymongo.errors import PyMongoError


class DocumentStore:
    """MongoDB persistence. Connection failures never fall back to volatile storage."""

    def __init__(self, mongo_url: str, db_name: str) -> None:
        self._client = MongoClient(mongo_url, serverSelectionTimeoutMS=2000)
        try:
            self._client.admin.command("ping")
        except (PyMongoError, OSError):
            self._client.close()
            raise RuntimeError("MongoDB 不可用，请启动数据库并检查连接配置") from None
        self._db = self._client[db_name]

    def ping(self) -> None:
        self._client.admin.command("ping")

    def close(self) -> None:
        self._client.close()

    def insert_one(self, collection: str, document: dict[str, Any]) -> dict[str, Any]:
        doc = self._stamp(copy.deepcopy(document), create=True)
        self._db[collection].insert_one(copy.deepcopy(doc))
        return doc

    def find_one(self, collection: str, query: dict[str, Any]) -> dict[str, Any] | None:
        return self._db[collection].find_one(query, {"_id": False})

    def find_many(
        self, collection: str, query: dict[str, Any] | None = None,
        sort: list[tuple[str, int]] | None = None, limit: int | None = None,
    ) -> list[dict[str, Any]]:
        cursor = self._db[collection].find(query or {}, {"_id": False})
        if sort:
            cursor = cursor.sort(sort)
        if limit:
            cursor = cursor.limit(limit)
        return list(cursor)

    def ensure_index(self, collection: str, keys: list[tuple[str, int]], **kwargs: Any) -> None:
        # Unique indexes enforce business invariants: failure must not be hidden.
        self._db[collection].create_index(keys, **kwargs)

    def update_one(
        self, collection: str, query: dict[str, Any], updates: dict[str, Any],
        upsert: bool = False,
    ) -> dict[str, Any] | None:
        return self._db[collection].find_one_and_update(
            query, {"$set": self._stamp(copy.deepcopy(updates), create=False)},
            upsert=upsert, return_document=ReturnDocument.AFTER, projection={"_id": False},
        )

    def delete_one(self, collection: str, query: dict[str, Any]) -> int:
        return int(self._db[collection].delete_one(query).deleted_count)

    def delete_many(self, collection: str, query: dict[str, Any]) -> int:
        return int(self._db[collection].delete_many(query).deleted_count)

    def clear(self) -> None:
        for name in self._db.list_collection_names():
            self._db[name].delete_many({})

    def _stamp(self, document: dict[str, Any], create: bool) -> dict[str, Any]:
        now = time.time()
        if create:
            document.setdefault("created_at", now)
        document["updated_at"] = now
        return document
