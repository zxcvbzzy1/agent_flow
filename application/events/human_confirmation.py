from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from application.services.events import EventStreamService


class HumanConfirmationService:
    """
    实现HumanApprovalProviderPort接口，提供request_approval方法供agent在需要人类确认时调用。
     - request_approval方法会创建一个confirmation请求，并将其发布到EventStreamService中，等待前端处理。
     - 前端处理后会调用resolve方法来更新请求的状态并通知等待的agent。
     - list_pending方法可以列出当前所有待处理的确认请求。
    """
    def __init__(self, streams: EventStreamService) -> None:
        self._streams = streams
        self._pending: dict[str, dict[str, dict[str, Any]]] = {}

    async def request_confirmation(
        self,
        *,
        run_id: str,
        agent_id: str,
        tool_name: str,
        called_event_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        confirmation_id = str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        item = {
            "confirmation_id": confirmation_id,
            "run_id": run_id,
            "agent_id": agent_id,
            "tool_name": tool_name,
            "called_event_name": called_event_name,
            "arguments": arguments,
            "status": "pending",
            "created_at": time.time(),
            "_future": future,
        }
        self._pending.setdefault(run_id, {})[confirmation_id] = item
        self._streams.publish(
            run_id,
            "human.confirmation.requested",
            self._public_item(item),
        )
        result = await future
        return result

    async def request_approval(
        self,
        *,
        run_id: str,
        agent_id: str,
        tool_name: str,
        called_event_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        return await self.request_confirmation(
            run_id=run_id,
            agent_id=agent_id,
            tool_name=tool_name,
            called_event_name=called_event_name,
            arguments=arguments,
        )

    def list_pending(self, run_id: str) -> list[dict[str, Any]]:
        return [
            self._public_item(item)
            for item in self._pending.get(run_id, {}).values()
            if item.get("status") == "pending"
        ]

    def resolve(
        self,
        *,
        run_id: str,
        confirmation_id: str,
        approved: bool,
        reason: str = "",
    ) -> dict[str, Any]:
        item = self._pending.get(run_id, {}).get(confirmation_id)
        if item is None:
            raise KeyError(f"confirmation not found: {confirmation_id}")
        if item.get("status") != "pending":
            return self._public_item(item)

        result = {
            "approved": approved,
            "reason": reason or ("用户已确认" if approved else "用户拒绝执行"),
        }
        item["status"] = "resolved"
        item["approved"] = approved
        item["reason"] = result["reason"]
        item["resolved_at"] = time.time()
        future = item.get("_future")
        if future is not None and not future.done():
            future.set_result(result)
        payload = self._public_item(item)
        self._streams.publish(run_id, "human.confirmation.resolved", payload)
        return payload

    def _public_item(self, item: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in item.items()
            if not key.startswith("_")
        }
