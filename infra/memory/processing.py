"""Redis processing state and leases; MongoDB contains acknowledged terminal results."""
import hashlib
import json
import os
import time
import uuid

from pymongo.errors import DuplicateKeyError
from redis import Redis
from redis.exceptions import WatchError

from domain.memory.long.processing import ProcessingConfig, ProcessingLeaseLost
from infra.memory.settings import MongoMemorySettings

BATCHES = "long_memory_batches"
CANDIDATES = "long_memory_candidates"
TERMINAL = {"completed", "failed"}
RUNTIME_FIELDS = {"owner", "steps", "extraction_groups", "publication_blocks", "outcome"}


def encode(value):
    return json.dumps(value, ensure_ascii=False)


def matches(row, query):
    return all(row.get(key) in value["$in"] if isinstance(value, dict) and "$in" in value
               else row.get(key) == value for key, value in query.items())


class MemoryProcessingRepository:
    def __init__(self, store, *, redis_client=None, runtime=None, lease_seconds=60):
        self.store = store
        self.user_settings = MongoMemorySettings(store)
        self.lease_seconds = lease_seconds
        self._owns_client = redis_client is None
        self.redis = redis_client if redis_client is not None else Redis.from_url(
            runtime.url if runtime else os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            decode_responses=True, socket_connect_timeout=5, socket_timeout=5)
        prefix = runtime.prefix if runtime else os.getenv("REDIS_KEY_PREFIX", "agenthub")
        # Database identity prevents cross-deployment collisions; one Cluster slot
        # makes ownership checks and checkpoint writes a single transaction.
        namespace = getattr(store, "event_namespace", "default")
        self.prefix = f"{prefix}:memory:{{{namespace}}}"
        self.live_key = f"{self.prefix}:batches"
        for collection, key in ((BATCHES, "batch_id"), (CANDIDATES, "candidate_id")):
            store.ensure_index(collection, [(key, 1)], unique=True)
        store.ensure_index(BATCHES, [("user_id", 1), ("status", 1), ("created_at", -1)])
        store.ensure_index(CANDIDATES, [("batch_id", 1), ("source_id", 1)])

    def close(self):
        if self._owns_client:
            self.redis.close()

    def start(self):
        self.redis.ping()
        self.user_settings.migrate_legacy()
        # Old workers must be stopped for the storage-format upgrade.
        self.store.delete_many("long_memory_processing_leases", {})

    def legacy_users(self):
        return {row["user_id"] for row in self.store.find_many(BATCHES, {"schema_version": {"$ne": 2}})}

    def lease_key(self, user_id):
        return f"{self.prefix}:lease:{hashlib.sha256(user_id.encode()).hexdigest()}"

    def candidate_key(self, batch_id):
        return f"{self.prefix}:candidates:{batch_id}"

    def settings(self, user_id):
        return self.user_settings.processing(user_id) or {
            "config": ProcessingConfig().to_dict(), "revision": "default-v1"}

    def save_settings(self, user_id, config):
        return self.user_settings.save_processing(user_id, {
            "config": ProcessingConfig(**config).to_dict(), "revision": str(uuid.uuid4())})

    def acquire(self, user_id, owner):
        return bool(self.redis.set(self.lease_key(user_id), owner, nx=True, ex=self.lease_seconds))

    def _lease_action(self, user_id, owner, release=False):
        key = self.lease_key(user_id)
        while True:
            with self.redis.pipeline() as pipe:
                try:
                    pipe.watch(key)
                    if pipe.get(key) != owner:
                        return False
                    pipe.multi()
                    pipe.delete(key) if release else pipe.expire(key, self.lease_seconds)
                    return bool(pipe.execute()[0])
                except WatchError:
                    continue

    def renew(self, user_id, owner):
        return self._lease_action(user_id, owner)

    def release(self, user_id, owner):
        return self._lease_action(user_id, owner, release=True)

    def _change(self, batch_id, owner, changes=None, *, initial=None, claim=False,
                candidate=None, candidate_changes=None, seed_candidates=(), acknowledge=False):
        """Fence every checkpoint/candidate write against user lease and batch owner."""
        key = self.candidate_key(batch_id)
        while True:
            with self.redis.pipeline() as pipe:
                try:
                    pipe.watch(self.live_key, key)
                    raw = pipe.hget(self.live_key, batch_id)
                    row = json.loads(raw) if raw else dict(initial or {})
                    if not row:
                        raise ProcessingLeaseLost("批次运行态不存在")
                    lease = self.lease_key(row["user_id"])
                    pipe.watch(lease)
                    if pipe.get(lease) != owner or (not claim and row.get("owner") != owner):
                        raise ProcessingLeaseLost("用户处理租约已失效或批次已转移")
                    if row["status"] == "finalizing" and not (claim or acknowledge):
                        raise ProcessingLeaseLost("批次终态已冻结，等待落库")
                    row.update(changes or {})
                    row.update(owner=owner, updated_at=time.time())
                    writes = {}
                    for item in [*seed_candidates, *([candidate] if candidate else [])]:
                        if not pipe.hexists(key, item["candidate_id"]):
                            writes[item["candidate_id"]] = encode(item)
                    if candidate_changes:
                        cid, updates = candidate_changes
                        current = pipe.hget(key, cid)
                        if not current:
                            raise ProcessingLeaseLost("候选运行态不存在")
                        writes[cid] = encode({**json.loads(current), **updates})
                    pipe.multi()
                    if acknowledge and row["status"] == "completed":
                        pipe.hdel(self.live_key, batch_id)
                        pipe.delete(key)
                    else:
                        # Unacknowledged work and retry checkpoints have no TTL.
                        pipe.hset(self.live_key, batch_id, encode(row))
                        if writes:
                            pipe.hset(key, mapping=writes)
                    pipe.execute()
                    return row
                except WatchError:
                    continue

    def batches(self, query):
        persisted_query = {"user_id": query["user_id"]} if "user_id" in query else {}
        rows = {b["batch_id"]: b for b in self.store.find_many(BATCHES, persisted_query)}
        for raw in self.redis.hvals(self.live_key):
            row = json.loads(raw)
            rows[row["batch_id"]] = row
        return sorted((row for row in rows.values() if matches(row, query)),
                      key=lambda row: (row["created_at"], row["batch_id"]))

    def page_batches(self, user_id, page, page_size):
        rows = sorted(self.batches({"user_id": user_id}), key=lambda b: (-b["created_at"], b["batch_id"]))
        return {"items": [{k: v for k, v in row.items() if k not in RUNTIME_FIELDS}
                          for row in rows[(page - 1) * page_size:page * page_size]],
                "total": len(rows), "page": page, "page_size": page_size}

    def put_batch(self, batch):
        return self._change(batch["batch_id"], batch["owner"],
                            initial={**batch, "attempt": 1, "schema_version": 2})

    def claim_batch(self, batch_id, owner):
        return self._change(batch_id, owner, claim=True)

    def update_batch(self, batch_id, owner, changes):
        return self._change(batch_id, owner, changes)

    def candidates(self, batch_id):
        rows = self.redis.hvals(self.candidate_key(batch_id))
        if rows:
            return sorted((json.loads(row) for row in rows), key=lambda c: c["candidate_id"])
        return self.store.find_many(CANDIDATES, {"batch_id": batch_id}, sort=[("candidate_id", 1)])

    def put_candidate(self, candidate, owner):
        self._change(candidate["batch_id"], owner, candidate=candidate)

    def update_candidate(self, candidate_id, changes, *, batch_id, owner):
        self._change(batch_id, owner, candidate_changes=(candidate_id, changes))

    def _archive(self, collection, key, row):
        """An older finalizer cannot overwrite a later retry's terminal result."""
        try:
            self.store.update_operators(collection, {key: row[key]}, {"$setOnInsert": row}, upsert=True)
        except DuplicateKeyError:
            pass
        self.store.update_operators(collection, {key: row[key], "$or": [
            {"attempt": {"$lt": row["attempt"]}}, {"attempt": {"$exists": False}},
        ]}, {"$set": row, "$unset": {field: "" for field in RUNTIME_FIELDS}})

    def finalize(self, batch_id, owner, memory_repository):
        batch = self.claim_batch(batch_id, owner)
        if batch["status"] != "finalizing":
            return
        outcome = batch["outcome"]

        def guard():
            if not self.renew(batch["user_id"], owner):
                raise ProcessingLeaseLost("落库时用户处理租约已失效")

        # A frozen Redis outbox survives partial Mongo writes. A successor replays
        # this exact result instead of re-running models and publishing new content.
        for candidate in self.candidates(batch_id):
            guard()
            self._archive(CANDIDATES, "candidate_id", {**candidate, "attempt": batch.get("attempt", 1)})
        for block in batch.get("publication_blocks", []):
            guard()
            memory_repository.put_block(block)
        guard()
        result = {k: v for k, v in batch.items() if k not in RUNTIME_FIELDS}
        result.update(status=outcome, stage="completed" if outcome == "completed" else batch["stage"])
        self._archive(BATCHES, "batch_id", result)
        if outcome == "completed":
            for sid in batch["source_ids"]:
                guard()
                memory_repository.update_source(sid, {"index_status": "ready", "error": ""})
        # Mongo acknowledgement precedes cleanup; failed checkpoints stay for retry.
        self._change(batch_id, owner, {"status": outcome, "publication_blocks": [], "outcome": ""}, acknowledge=True)

    def migrate_legacy(self, user_id, owner):
        """Import old checkpoints before removing nonterminal Mongo documents.

        Deploy with old workers stopped: the old Mongo lease is no longer used.
        """
        rows = self.store.find_many(BATCHES, {"user_id": user_id, "schema_version": {"$ne": 2}})
        for row in rows:
            if not self.renew(user_id, owner):
                raise ProcessingLeaseLost("迁移时处理租约已失效")
            candidates = self.store.find_many(CANDIDATES, {"batch_id": row["batch_id"]})
            migrated = {**row, "owner": owner, "attempt": row.get("attempt", 1), "schema_version": 2}
            if row["status"] != "completed":
                self._change(row["batch_id"], owner, initial=migrated, claim=True, seed_candidates=candidates)
            if row["status"] in TERMINAL:
                result = {k: v for k, v in migrated.items() if k not in RUNTIME_FIELDS}
                self._archive(BATCHES, "batch_id", result)
            else:
                self.store.delete_one(BATCHES, {"batch_id": row["batch_id"], "schema_version": {"$ne": 2}})
                self.store.delete_many(CANDIDATES, {"batch_id": row["batch_id"], "attempt": {"$exists": False}})

    def retry(self, user_id, batch_id):
        rows = self.batches({"user_id": user_id, "batch_id": batch_id})
        if not rows:
            raise KeyError(batch_id)
        owner = str(uuid.uuid4())
        if not self.acquire(user_id, owner):
            raise ValueError("当前用户已有处理任务，请稍后重试")
        try:
            self.migrate_legacy(user_id, owner)
            row = self.batches({"user_id": user_id, "batch_id": batch_id})[0]
            if row["status"] != "failed":
                raise ValueError("只有失败批次可以重试；运行中或已完成批次不能重复触发")
            return self._change(batch_id, owner, {"status": "pending", "error": "", "outcome": "",
                "attempt": row.get("attempt", 1) + 1, "retry_at": time.time()},
                initial={"steps": {}, "category_progress": {}, **row}, claim=True,
                seed_candidates=self.candidates(batch_id))
        finally:
            self.release(user_id, owner)
