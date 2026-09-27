"""Durable checkpoints and per-user leases; no Mongo TTL history deletion."""
import time
import uuid

from domain.memory.long.processing import ProcessingConfig, ProcessingLeaseLost

BATCHES = "long_memory_batches"
CANDIDATES = "long_memory_candidates"
SETTINGS = "long_memory_processing_settings"
LEASES = "long_memory_processing_leases"


class MongoProcessingRepository:
    def __init__(self, store):
        self.store = store
        for collection, key in ((BATCHES, "batch_id"), (CANDIDATES, "candidate_id"),
                                (SETTINGS, "user_id"), (LEASES, "user_id")):
            store.ensure_index(collection, [(key, 1)], unique=True)
        store.ensure_index(BATCHES, [("user_id", 1), ("status", 1), ("created_at", -1)])
        store.ensure_index(CANDIDATES, [("batch_id", 1), ("source_id", 1)])

    def settings(self, user_id):
        row = self.store.find_one(SETTINGS, {"user_id": user_id})
        return {"config": row["config"], "revision": row["revision"]} if row else {
            "config": ProcessingConfig().to_dict(), "revision": "default-v1"}

    def save_settings(self, user_id, config):
        config = ProcessingConfig(**config).to_dict()
        return self.store.update_one(SETTINGS, {"user_id": user_id},
            {"config": config, "revision": str(uuid.uuid4())}, upsert=True)

    def batches(self, query):
        return self.store.find_many(BATCHES, query, sort=[("created_at", 1), ("batch_id", 1)])

    def page_batches(self, user_id, page, page_size):
        query = {"user_id": user_id}
        return {"items": self.store.find_many(BATCHES, query, sort=[("created_at", -1), ("batch_id", 1)],
                    skip=(page - 1) * page_size, limit=page_size, projection={"steps": False, "owner": False}),
                "total": self.store.count(BATCHES, query), "page": page, "page_size": page_size}

    def put_batch(self, batch):
        return self.store.update_operators(BATCHES, {"batch_id": batch["batch_id"]},
            {"$setOnInsert": batch}, upsert=True)

    def claim_batch(self, batch_id, owner):
        return self.store.update_one(BATCHES, {"batch_id": batch_id}, {"owner": owner})

    def update_batch(self, batch_id, owner, changes):
        result = self.store.update_one(BATCHES, {"batch_id": batch_id, "owner": owner}, changes)
        if not result:
            raise ProcessingLeaseLost("批次租约已转移")
        return result

    def candidates(self, batch_id):
        return self.store.find_many(CANDIDATES, {"batch_id": batch_id}, sort=[("candidate_id", 1)])

    def put_candidate(self, candidate):
        return self.store.update_operators(CANDIDATES, {"candidate_id": candidate["candidate_id"]},
            {"$setOnInsert": candidate}, upsert=True)

    def update_candidate(self, candidate_id, changes):
        self.store.update_one(CANDIDATES, {"candidate_id": candidate_id}, changes)

    def acquire(self, user_id, owner):
        from pymongo.errors import DuplicateKeyError
        try:
            self.store.update_operators(LEASES, {"user_id": user_id},
                {"$setOnInsert": {"user_id": user_id, "owner": "", "expires_at": 0}}, upsert=True)
        except DuplicateKeyError:
            pass
        return bool(self.store.update_one(LEASES, {"user_id": user_id, "expires_at": {"$lte": time.time()}},
                                           {"owner": owner, "expires_at": time.time() + 60}))

    def renew(self, user_id, owner):
        return bool(self.store.update_one(LEASES, {"user_id": user_id, "owner": owner,
            "expires_at": {"$gt": time.time()}}, {"expires_at": time.time() + 60}))

    def release(self, user_id, owner):
        self.store.update_one(LEASES, {"user_id": user_id, "owner": owner}, {"expires_at": 0})

    def retry(self, user_id, batch_id):
        row = self.store.find_one(BATCHES, {"user_id": user_id, "batch_id": batch_id})
        if not row:
            raise KeyError(batch_id)
        result = self.store.update_one(BATCHES, {"user_id": user_id, "batch_id": batch_id, "status": "failed"},
            {"status": "pending", "error": "", "owner": "", "retry_at": time.time()})
        if not result:
            raise ValueError("只有失败批次可以重试；运行中或已完成批次不能重复触发")
        return result
