"""One per-user document for retrieval and generation preferences."""
import time
from pymongo.errors import DuplicateKeyError

SETTINGS = "long_memory_settings"
LEGACY_PROCESSING_SETTINGS = "long_memory_processing_settings"


class MongoMemorySettings:
    def __init__(self, store, *, ensure_indexes=True):
        self.store = store
        if ensure_indexes:
            store.ensure_index(SETTINGS, [("user_id", 1)], unique=True)

    def _ensure_user(self, user_id):
        try:
            self.store.update_operators(SETTINGS, {"user_id": user_id},
                {"$setOnInsert": {"user_id": user_id}}, upsert=True)
        except DuplicateKeyError:
            # Concurrent first saves must update separate sections of the same row.
            pass

    def get(self, user_id):
        row = self.store.find_one(SETTINGS, {"user_id": user_id}) or {}
        if "config" in row and "retrieval" not in row:
            self.store.update_operators(SETTINGS, {"user_id": user_id, "retrieval": {"$exists": False}},
                {"$set": {"retrieval": row["config"]}, "$unset": {"config": ""}})
        elif "config" in row:
            self.store.update_operators(SETTINGS, {"user_id": user_id, "retrieval": {"$exists": True}},
                                        {"$unset": {"config": ""}})
        legacy = self.store.find_one(LEGACY_PROCESSING_SETTINGS, {"user_id": user_id})
        if "processing" not in row and legacy:
            self._ensure_user(user_id)
            # Preserve the revision: already queued sources keep their grouping.
            self.store.update_one(SETTINGS, {"user_id": user_id, "processing": {"$exists": False}},
                {"processing": {"config": legacy["config"], "revision": legacy["revision"]}})
        saved = self.store.find_one(SETTINGS, {"user_id": user_id}) or {}
        if legacy and "processing" in saved:
            # Copy/acknowledge before deleting the exact legacy revision. A newer
            # canonical setting is authoritative; never overwrite it during migration.
            self.store.delete_one(LEGACY_PROCESSING_SETTINGS,
                                  {"user_id": user_id, "revision": legacy["revision"]})
        return saved

    def migrate_legacy(self):
        users = {row["user_id"] for row in self.store.find_many(LEGACY_PROCESSING_SETTINGS, {})}
        users.update(row["user_id"] for row in self.store.find_many(SETTINGS, {"config": {"$exists": True}}))
        for user_id in users:
            self.get(user_id)

    def retrieval(self, user_id):
        return self.get(user_id).get("retrieval")

    def processing(self, user_id):
        return self.get(user_id).get("processing")

    def save_retrieval(self, user_id, config):
        self._ensure_user(user_id)
        return self.store.update_operators(SETTINGS, {"user_id": user_id},
            {"$set": {"retrieval": config, "updated_at": time.time()}, "$unset": {"config": ""}})

    def save_processing(self, user_id, snapshot):
        self._ensure_user(user_id)
        return self.store.update_one(SETTINGS, {"user_id": user_id}, {"processing": snapshot})
