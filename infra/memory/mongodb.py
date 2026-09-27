from __future__ import annotations

import time

from infra.db.mongodb import DocumentStore

SOURCES = "long_memory_sources"
BLOCKS = "long_memory_blocks"


class MongoMemoryRepository:
    def __init__(self, store: DocumentStore, *, ensure_indexes: bool = True):
        self.store = store
        if ensure_indexes:
            store.ensure_index(SOURCES, [("source_id", 1)], unique=True)
            store.ensure_index(BLOCKS, [("block_id", 1)], unique=True)
            store.ensure_index(BLOCKS, [("memory_id", 1), ("version", 1)], unique=True)
            store.ensure_index(BLOCKS, [("user_id", 1), ("status", 1), ("expires_at", 1)])
            store.ensure_index(BLOCKS, [("source_id", 1)])
            store.ensure_index(BLOCKS, [("derived_from_block_ids", 1)])
            store.ensure_index(BLOCKS, [("evidence_source_ids", 1)])
            store.ensure_index(BLOCKS, [("batch_id", 1)])
            store.ensure_index(SOURCES, [("index_status", 1), ("user_id", 1), ("created_at", 1)])
            for name in ("run_id", "message_id", "conversation_id", "room_id"):
                store.ensure_index(SOURCES, [(name, 1)])

    def get_source(self, source_id: str) -> dict | None:
        return self.store.find_one(SOURCES, {"source_id": source_id})

    def completed_batches(self, user_id: str) -> set[str]:
        return {b["batch_id"] for b in self.store.find_many("long_memory_batches",
                {"user_id": user_id, "status": "completed"}, projection={"steps": False})}

    def sources(self, query: dict) -> list[dict]:
        return self.store.find_many(SOURCES, query)

    def put_source(self, document: dict) -> dict:
        return self.store.update_operators(SOURCES, {"source_id": document["source_id"]},
                                           {"$setOnInsert": document}, upsert=True)

    def update_source(self, source_id: str, changes: dict) -> None:
        self.store.update_one(SOURCES, {"source_id": source_id}, changes)

    def blocks(self, query: dict) -> list[dict]:
        return self.store.find_many(BLOCKS, query, projection={"_usage_run_ids": False})

    def put_block(self, document: dict) -> dict:
        return self.store.update_operators(BLOCKS, {"block_id": document["block_id"]},
                                           {"$setOnInsert": document}, upsert=True)

    def update_block(self, block_id: str, changes: dict) -> None:
        self.store.update_one(BLOCKS, {"block_id": block_id}, changes)

    def mark_used(self, block_ids: list[str], run_id: str) -> None:
        # Atomic per block/run: multiple executors or repeated preparation count once.
        for block_id in set(block_ids):
            self.store.update_operators(BLOCKS, {"block_id": block_id, "_usage_run_ids": {"$ne": run_id}},
                {"$inc": {"usage_count": 1}, "$set": {"last_used_at": time.time()},
                 "$addToSet": {"_usage_run_ids": run_id}})
