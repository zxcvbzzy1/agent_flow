"""Non-fatal long-term memory integration shared by orchestration and DM runs."""
from __future__ import annotations

import asyncio
import logging

from domain.memory.long.models import MemoryScope
from domain.memory.long.providers import LongTermMemoryProvider

log = logging.getLogger(__name__)


class RunMemoryCoordinator:
    def __init__(self, memory, store, events):
        self.memory, self.store, self.events = memory, store, events

    def _status(self, run_id: str, **changes):
        try:
            self.store.update_one("runs", {"run_id": run_id}, changes)
        except Exception:
            log.exception("Cannot record long-term memory status for %s", run_id)

    async def prepare(self, record: dict) -> list[dict]:
        scope = record.get("memory_scope")
        if self.memory is None or not scope or not scope.get("user_id"):
            self._status(record["run_id"], memory_status="skipped", memory_reason="missing_trusted_user_scope")
            return []
        try:
            return await asyncio.to_thread(self.memory.recall, MemoryScope(**scope),
                record.get("user_question", record.get("prompt", "")), run_id=record["run_id"], record_usage=False)
        except Exception as exc:
            log.exception("Long-term recall failed for %s", record["run_id"])
            self._status(record["run_id"], memory_recall_error=str(exc))
            return []

    async def inject(self, agent, blocks: list[dict], run_id: str) -> None:
        agent.states["long_term_memory"] = list(blocks)
        enabled = any(isinstance(provider, LongTermMemoryProvider) and provider.enabled
                      for provider in agent.context_engine._providers)
        if enabled and blocks and self.memory is not None:
            try:
                await asyncio.to_thread(self.memory.repository.mark_used, [b["block_id"] for b in blocks], run_id)
            except Exception:
                log.exception("Long-term usage accounting failed for %s", run_id)

    async def archive(self, record: dict) -> None:
        scope = record.get("memory_scope")
        if self.memory is None or not scope or not scope.get("user_id"):
            self._status(record["run_id"], memory_status="skipped", memory_reason="missing_trusted_user_scope")
            return
        if record.get("memory_status") == "ready":
            return
        try:
            events = await self.events.list_events(record["run_id"], user_id=scope["user_id"])
            source = await asyncio.to_thread(self.memory.archive_run, record, events)
            self._status(record["run_id"], memory_status=source["index_status"], memory_source_id=source["source_id"], memory_error="")
        except Exception as exc:
            log.exception("Long-term archive failed for %s", record["run_id"])
            self._status(record["run_id"], memory_status="failed", memory_error=str(exc))
