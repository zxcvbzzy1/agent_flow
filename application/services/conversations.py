from __future__ import annotations

import uuid
from typing import Any

from infra.db.mongodb import DocumentStore


class ConversationService:
    def __init__(self, store: DocumentStore, runtime=None) -> None:
        self._store = store
        self.runtime = runtime

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

    async def delete_conversation(self, conversation_id: str) -> dict[str, Any]:
        conversation = self.get_conversation(conversation_id)
        if conversation is None:
            raise KeyError(f"会话不存在: {conversation_id}")

        messages = self.list_messages(conversation_id)
        runs = self._store.find_many("runs", {"conversation_id": conversation_id})
        run_ids = {
            item.get("run_id")
            for item in [conversation, *messages, *runs]
            if item.get("run_id")
        }

        stats = {
            "conversations": 0,
            "messages": 0,
            "runs": 0,
            "events": 0,
        }
        for run_id in run_ids:
            if self.runtime:
                from application.services.run_state import RunStateService
                deleted = await RunStateService(self._store, self.runtime).delete(run_id)
                stats["runs"] += deleted["runs"]
                stats["events"] += deleted["events"]
            else:
                stats["runs"] += self._store.delete_many("runs", {"run_id": run_id})
                stats["events"] += self._store.delete_many("events", {"run_id": run_id})
        stats["conversations"] = self._store.delete_one("conversations", {"conversation_id": conversation_id})
        stats["messages"] = self._store.delete_many("messages", {"conversation_id": conversation_id})
        return {"deleted": True, "conversation_id": conversation_id, "stats": stats}

    async def add_message(
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

    def get_message(self, conversation_id: str, message_id: str) -> dict[str, Any]:
        message = self._store.find_one("messages", {"message_id": message_id})
        if message is None or message.get("conversation_id") != conversation_id:
            raise KeyError(f"消息不存在: {message_id}")
        return message

    def attach_message_run(self, conversation_id: str, message_id: str, run_id: str) -> None:
        self.get_message(conversation_id, message_id)
        self._store.update_one(
            "messages",
            {"message_id": message_id},
            {"run_id": run_id},
        )
