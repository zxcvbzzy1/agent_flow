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
        return all(document.get(key) == value for key, value in query.items())

