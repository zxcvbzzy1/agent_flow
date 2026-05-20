from __future__ import annotations

import time
import uuid
from typing import Any

from infra.db.mongodb import DocumentStore


class ConversationService:
    QUEUE_STATUSES = {"pending", "processing", "done", "failed", "cancelled"}

    def __init__(self, store: DocumentStore) -> None:
        self._store = store

    def create_conversation(
        self,
        title: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        conversation_id = str(uuid.uuid4())
        record = {
            "conversation_id": conversation_id,
            "title": title or "新对话",
            "metadata": metadata or {},
        }
        return self._store.insert_one("conversations", record)

    def list_conversations(self) -> list[dict[str, Any]]:
        return self._store.find_many("conversations", sort=[("created_at", -1)])

    def get_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        return self._store.find_one("conversations", {"conversation_id": conversation_id})

    def delete_conversation(self, conversation_id: str) -> dict[str, Any]:
        conversation = self.get_conversation(conversation_id)
        if conversation is None:
            raise KeyError(f"会话不存在: {conversation_id}")

        messages = self.list_messages(conversation_id)
        queue_items = self.list_queue(conversation_id)
        runs = self._store.find_many("runs", {"conversation_id": conversation_id})
        run_ids = {
            item.get("run_id")
            for item in [conversation, *messages, *queue_items, *runs]
            if item.get("run_id")
        }

        stats = {
            "conversations": self._store.delete_one("conversations", {"conversation_id": conversation_id}),
            "messages": self._store.delete_many("messages", {"conversation_id": conversation_id}),
            "message_queue": self._store.delete_many("message_queue", {"conversation_id": conversation_id}),
            "runs": self._store.delete_many("runs", {"conversation_id": conversation_id}),
            "events": 0,
        }
        for run_id in run_ids:
            stats["events"] += self._store.delete_many("events", {"run_id": run_id})
        return {"deleted": True, "conversation_id": conversation_id, "stats": stats}

    def add_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        if self.get_conversation(conversation_id) is None:
            raise KeyError(f"会话不存在: {conversation_id}")
        message = {
            "message_id": str(uuid.uuid4()),
            "conversation_id": conversation_id,
            "role": role,
            "content": content,
            "metadata": metadata or {},
            "run_id": run_id,
        }
        return self._store.insert_one("messages", message)

    def list_messages(self, conversation_id: str) -> list[dict[str, Any]]:
        return self._store.find_many(
            "messages",
            {"conversation_id": conversation_id},
            sort=[("created_at", 1)],
        )

    def enqueue_message(
        self,
        conversation_id: str,
        message_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        message = self._resolve_message(conversation_id, message_id)
        queue_item = {
            "queue_id": str(uuid.uuid4()),
            "conversation_id": conversation_id,
            "message_id": message["message_id"],
            "status": "pending",
            "metadata": metadata or {},
            "run_id": None,
            "processed_at": None,
        }
        return self._store.insert_one("message_queue", queue_item)

    def list_queue(self, conversation_id: str) -> list[dict[str, Any]]:
        return self._store.find_many(
            "message_queue",
            {"conversation_id": conversation_id},
            sort=[("created_at", 1)],
        )

    def latest_pending_user_message(self, conversation_id: str) -> dict[str, Any]:
        pending_items = [
            item for item in self.list_queue(conversation_id)
            if item.get("status") == "pending"
        ]
        if pending_items:
            message_id = pending_items[-1]["message_id"]
            message = self._store.find_one("messages", {"message_id": message_id})
            if message:
                return message
        messages = [
            item for item in self.list_messages(conversation_id)
            if item.get("role") == "user"
        ]
        if not messages:
            raise KeyError("没有可用于创建 run 的用户消息")
        return messages[-1]

    def mark_queue_processing(self, conversation_id: str, message_id: str, run_id: str) -> None:
        queue_items = [
            item for item in self.list_queue(conversation_id)
            if item.get("message_id") == message_id and item.get("status") == "pending"
        ]
        if not queue_items:
            return
        self._store.update_one(
            "message_queue",
            {"queue_id": queue_items[-1]["queue_id"]},
            {"status": "processing", "run_id": run_id, "processed_at": time.time()},
        )

    def _resolve_message(self, conversation_id: str, message_id: str | None) -> dict[str, Any]:
        if message_id:
            message = self._store.find_one("messages", {"message_id": message_id})
            if message is None:
                raise KeyError(f"消息不存在: {message_id}")
            return message
        messages = [
            item for item in self.list_messages(conversation_id)
            if item.get("role") == "user"
        ]
        if not messages:
            raise KeyError("没有可入队的用户消息")
        return messages[-1]
