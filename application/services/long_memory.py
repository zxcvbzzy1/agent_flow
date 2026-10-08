"""Coordinates immutable L3 files, L2 lifecycle and budgeted L1 retrieval."""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from domain.memory.long.chunking import EstimatedTokenCounter, LineChunker
from domain.memory.long.models import MemoryBlock, MemoryScope, MemorySource
from domain.memory.long.ports import AcceptAllRouter, MemoryRepository, MemoryRetriever, MemoryRouter, SourceFiles, TokenCounter
from domain.memory.long.providers import LongTermMemoryProvider
from domain.memory.long.retrieval import BM25Retriever, RecallOptions, RecallResult, effective_status

from infra.memory.files import MarkdownSourceFiles
from infra.memory.mongodb import MongoMemoryRepository
from infra.memory.processing import MemoryProcessingRepository
from infra.memory.jev import JevClassifier
from infra.memory.extraction import LLMExtractionAdapter

_UNSET = object()


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def stable_id(value: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "agent-flow-memory:" + value))


def component(value: str) -> str:
    # quote() preserves dots, so encode those too to prevent '.' / '..' segments.
    return quote(value, safe="").replace(".", "%2E")


class LongTermMemoryService:
    def __init__(self, repository: MemoryRepository, files: SourceFiles, *,
                 router: MemoryRouter | None = None, retriever: MemoryRetriever | None = None,
                 counter: TokenCounter | None = None, chunk_tokens: int = 1000,
                 retriever_factory: Callable[[RecallOptions], MemoryRetriever] | None = None,
                 settings_loader: Callable[[str], dict | None] | None = None):
        self.repository, self.files = repository, files
        self.router = router or AcceptAllRouter()
        self.retriever = retriever
        self.retriever_factory = retriever_factory or (lambda config: BM25Retriever(config.k1, config.b))
        self.settings_loader = settings_loader
        self.counter = counter or EstimatedTokenCounter()
        self.chunker = LineChunker(self.counter, chunk_tokens)
        self.provider = LongTermMemoryProvider()
        # The low-level service also supports rules-only hosts. The production
        # factory installs processing and defaults newly archived runs to JEV.
        self.processing = None

    def _path(self, scope: MemoryScope, folder: str, name: str) -> str:
        if not scope.user_id or not scope.conversation_id:
            raise ValueError("长期记忆需要用户和会话标识")
        return "/".join(component(value) for value in (
            scope.user_id, scope.room_id or "standalone", scope.conversation_id, folder, name)) + ".md"

    def archive_run(self, record: dict, events: list[dict]) -> dict | None:
        raw_scope = record.get("memory_scope")
        if not raw_scope or not raw_scope.get("user_id"):
            return None
        if record.get("status") not in {"finished", "failed", "cancelled"}:
            raise ValueError("只能归档已结束的 run")
        scope = MemoryScope(**raw_scope)
        source_id = stable_id("run:" + record["run_id"])
        existing = self.repository.get_source(source_id)
        if existing:
            return self.reindex_source(source_id) if existing["index_status"] != "ready" else existing
        lines = ["# Run 长期记忆", "", json.dumps({
            **scope.to_dict(), "run_id": record["run_id"], "status": record["status"],
            "started_at": record.get("started_at"), "finished_at": record.get("finished_at"),
            "error": record.get("error", ""), "cancel_reason": record.get("cancel_reason", ""),
        }, ensure_ascii=False), ""]
        sections = []

        def append_section(content: str, kind: str, event: dict | None = None):
            start = len(lines) + 1
            lines.extend(content.replace("\r\n", "\n").replace("\r", "\n").split("\n"))
            sections.append({"start_line": start, "end_line": len(lines),
                             "section_kind": kind, "event": event or {}})
            lines.append("")

        append_section(f"## 用户问题\n{record.get('user_question', record.get('prompt', ''))}\n\n"
                       f"## 最终回答\n{record.get('final', '')}", "qa")
        seen = set()
        # Existing history is already ordered; stable sorting retains order for equal timestamps.
        for event in sorted(events, key=lambda item: item.get("created_at", 0)):
            if event.get("name") != "agent.think" or event.get("run_id") != record["run_id"]:
                continue
            if event["event_id"] in seen:
                continue
            seen.add(event["event_id"])
            content = (event.get("payload") or {}).get("think", "")
            if not content:
                continue
            trace = {key: event.get(key, "") for key in ("event_id", "execution_id", "agent_id", "created_at")}
            lines.extend(["## 历史思考记录", json.dumps(trace, ensure_ascii=False)])
            append_section(str(content), "think", trace)
        text = "\n".join(lines) + "\n"
        source = MemorySource(source_id=source_id, **scope.to_dict(),
            file_path=self._path(scope, "runs", record["run_id"]), content_hash=digest(text),
            run_id=record["run_id"], message_id=record.get("source_message_id") or record.get("message_id") or "",
            run_status=record["status"], sections=sections).to_dict()
        if self.processing:
            snapshot = self.processing.repository.settings(scope.user_id)
            source.update(processing_config=snapshot["config"], processing_revision=snapshot["revision"])
            if snapshot["config"]["mode"] == "jev":
                source["index_status"] = "queued"
        return self._store_source(source, text)

    def _store_source(self, source: dict, text: str) -> dict:
        self.files.write(source["file_path"], text)
        self.repository.put_source(source)
        return self.reindex_source(source["source_id"])

    def reindex_source(self, source_id: str) -> dict:
        source = self.repository.get_source(source_id)
        if source is None:
            raise KeyError(source_id)
        if source.get("deleted_at") is not None:
            return source
        if source.get("processing_config", {}).get("mode") == "jev":
            # Raw candidates must never bypass JEV, including explicit reindex/retry.
            return source
        try:
            text = self.files.read(source["file_path"])
            if digest(text) != source["content_hash"]:
                raise ValueError("记忆原文校验失败，不能覆盖原有行号索引")
            candidates = []
            for section in source["sections"]:
                if source["source_kind"] == "run":
                    candidates.extend(self.chunker.split(text, section))
                else:
                    candidates.append({**section, "content": "\n".join(
                        text.splitlines()[section["start_line"] - 1:section["end_line"]])})
            for index, candidate in enumerate(candidates):
                block_id = stable_id(f"{source_id}:{index}")
                # Reindex is insert-only; never revive lifecycle changes or overwrite metadata.
                if self.repository.blocks({"block_id": block_id}):
                    continue
                decision = self.router.route(source, candidate)
                if not decision.include:
                    continue
                content = candidate["content"]
                fields = {key: source[key] for key in (
                    "source_id", "user_id", "room_id", "conversation_id", "file_path", "run_id", "message_id", "run_status")}
                block = MemoryBlock(block_id=block_id, memory_id=block_id, **fields,
                    start_line=candidate["start_line"], end_line=candidate["end_line"], content=content,
                    content_hash=digest(content), token_count=self.counter.count(content),
                    token_count_method=self.counter.name, section_kind=candidate["section_kind"],
                    event=candidate.get("event", {}), memory_type=decision.memory_type,
                    tags=decision.tags, expires_at=decision.expires_at).to_dict()
                block.update(source.get("block_defaults", {}))
                self.repository.put_block(block)
            # A retry also completes a derived operation interrupted after inserting its block.
            new_ids = [b["block_id"] for b in self.repository.blocks({"source_id": source_id})]
            if source["source_kind"] in {"update", "aggregate"} and new_ids:
                status = "superseded" if source["source_kind"] == "update" else "merged"
                for parent_id in source["block_defaults"].get("derived_from_block_ids", []):
                    parents = self.repository.blocks({"block_id": parent_id})
                    if parents and parents[0]["status"] == "active":
                        self.repository.update_block(parent_id, {"status": status, "superseded_by_block_ids": new_ids})
            self.repository.update_source(source_id, {"index_status": "ready", "error": ""})
        except Exception as exc:
            self.repository.update_source(source_id, {"index_status": "failed", "error": str(exc)})
            raise
        return self.repository.get_source(source_id)

    def get_recall_options(self, user_id: str) -> RecallOptions:
        settings = self.settings_loader(user_id) if user_id and self.settings_loader else None
        return RecallOptions(**(settings or {}))

    def recall(self, scope: MemoryScope, query: str, *, run_id: str, limit: int | None = None,
               token_budget: int | None = None, record_usage: bool = True,
               config: RecallOptions | None = None) -> list[dict]:
        if not scope.user_id or (limit is not None and limit <= 0) or (token_budget is not None and token_budget <= 0):
            return []
        result = self.recall_result(scope, query, config=config, limit=limit, token_budget=token_budget)
        if run_id and record_usage:
            self.repository.mark_used([b["block_id"] for b in result.selected], run_id)
        return result.selected

    def recall_result(self, scope: MemoryScope, query: str, *, config: RecallOptions | None = None,
                      limit: int | None = None, token_budget: int | None = None) -> RecallResult:
        """Pure read path shared by previews and runs; never accounts usage."""
        effective = config or self.get_recall_options(scope.user_id)
        # Legacy internal limit/budget arguments remain supported. HTTP settings
        # and IM preview drafts are validated before being passed in as options.
        selection_limit = effective.limit if limit is None else limit
        budget = effective.token_budget if token_budget is None else token_budget
        result = RecallResult(config={**effective.to_dict(), "limit": selection_limit, "token_budget": budget},
                              token_count_method=self.counter.name)
        now = time.time()
        sources = {s["source_id"]: s for s in self.repository.sources({"user_id": scope.user_id})} if scope.user_id else {}
        completed = self.repository.completed_batches(scope.user_id) if scope.user_id else set()
        blocks = [b for b in self.repository.blocks({"user_id": scope.user_id, "status": "active"})
                  if effective_status(b, sources.get(b["source_id"]), now, sources=sources,
                                      completed_batches=completed) == "active"] if scope.user_id else []
        result.corpus_count = len(blocks)
        retriever = self.retriever if self.retriever is not None else self.retriever_factory(effective)
        ranked = retriever.rank(query, blocks)
        result.matched_count = len(ranked)
        result.diagnostics_truncated = len(ranked) > 100
        for rank, block in enumerate(ranked, 1):
            candidate = result.selected + [block]
            rendered = "\n\n".join(self.provider.get({"long_term_memory": candidate}))
            if len(result.selected) >= selection_limit:
                reason = "limit"
            elif self.counter.count(rendered) > budget:
                reason = "token_budget"
            else:
                reason = "selected"
                result.selected.append({**block, "rank": rank})
                result.context = rendered
            if rank <= 100:
                result.diagnostics.append({**{key: block[key] for key in (
                    "block_id", "score", "file_path", "start_line", "end_line", "token_count")},
                    "rank": rank, "reason": reason, "summary": block["content"][:240]})
        result.token_count = self.counter.count(result.context)
        if not result.selected:
            result.empty_reason = "no_memory" if not blocks else "no_match" if not ranked else "token_budget"
        return result

    def _owned_block(self, user_id: str, block_id: str, *, active: bool = False) -> dict:
        blocks = self.repository.blocks({"user_id": user_id, "block_id": block_id})
        if not blocks:
            raise KeyError(block_id)
        block = blocks[0]
        source = self.repository.get_source(block["source_id"])
        sources = {s["source_id"]: s for s in self.repository.sources({"user_id": user_id})}
        if active and effective_status(block, source, time.time(), sources=sources,
                completed_batches=self.repository.completed_batches(user_id)) != "active":
            raise ValueError("只能更新或合并有效记忆")
        return block

    def update_memory(self, user_id: str, block_id: str, *, content: str | None = None,
                      tags: list[str] | None = None, metadata: dict | None = None,
                      expires_at: float | None | object = _UNSET) -> dict:
        """Omitted expires_at stays unchanged; explicit None removes expiration."""
        block = self._owned_block(user_id, block_id, active=True)
        changes = {}
        if tags is not None:
            changes["tags"] = list(tags)
        if metadata is not None:
            changes["metadata"] = dict(metadata)
        if expires_at is not _UNSET:
            changes["expires_at"] = expires_at
        if content is None:
            self.repository.update_block(block_id, changes)
            return self._owned_block(user_id, block_id)
        defaults = {key: block[key] for key in ("memory_id", "memory_type", "tags", "metadata", "expires_at")}
        defaults.update(changes, version=block["version"] + 1)
        return self._derive([block], content, MemoryScope(user_id, block["room_id"], block["conversation_id"]),
                            "update", defaults)

    def aggregate_memories(self, user_id: str, block_ids: list[str], content: str,
                           target: MemoryScope, *, tags: list[str] | None = None) -> dict:
        if target.user_id != user_id or len(set(block_ids)) < 2:
            raise ValueError("聚合需要同一用户的至少两个不同块")
        blocks = [self._owned_block(user_id, bid, active=True) for bid in dict.fromkeys(block_ids)]
        expirations = [b["expires_at"] for b in blocks if b.get("expires_at") is not None]
        return self._derive(blocks, content, target, "aggregate", {
            "tags": tags or [], "expires_at": min(expirations) if expirations else None})

    def _derive(self, parents: list[dict], content: str, scope: MemoryScope, kind: str, defaults: dict) -> dict:
        if not content.strip():
            raise ValueError("记忆内容不能为空")
        source_id = str(uuid.uuid4())
        content = content.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
        lines = content.split("\n")
        statuses = sorted({b["run_status"] for b in parents})
        defaults = {**defaults, "derived_from_block_ids": [b["block_id"] for b in parents],
                    "evidence_refs": [r for b in parents for r in (b.get("evidence_refs") or [{
                        k: b[k] for k in ("source_id", "file_path", "start_line", "end_line", "room_id",
                                         "conversation_id", "run_id", "run_status", "event", "section_kind")}])],
                    "evidence_source_ids": list(dict.fromkeys(sid for b in parents for sid in (
                        [b["source_id"]] + b.get("evidence_source_ids", [])))),
                    "metadata": {**defaults.get("metadata", {}), "source_run_statuses": statuses}}
        source = MemorySource(source_id=source_id, **scope.to_dict(), source_kind=kind,
            file_path=self._path(scope, "derived", source_id), content_hash=digest(content + "\n"),
            run_status=", ".join(statuses), block_defaults=defaults,
            sections=[{"start_line": 1, "end_line": len(lines), "section_kind": "derived"}]).to_dict()
        self._store_source(source, content + "\n")
        blocks = self.repository.blocks({"source_id": source_id})
        if not blocks:
            raise ValueError("路由未接纳派生记忆")
        return blocks[0]

    def delete_memory(self, user_id: str, block_id: str) -> int:
        self._owned_block(user_id, block_id)
        return self._invalidate([block_id], "deleted")

    def expire_memory(self, user_id: str, block_id: str) -> int:
        self._owned_block(user_id, block_id)
        return self._invalidate([block_id], "expired")

    def _invalidate(self, block_ids: list[str], status: str) -> int:
        pending, visited = list(block_ids), set()
        now = time.time()
        while pending:
            block_id = pending.pop()
            if block_id in visited:
                continue
            visited.add(block_id)
            rows = self.repository.blocks({"block_id": block_id})
            if not rows:
                continue
            block = rows[0]
            changes = {"status": status, "deleted_at": now} if status == "deleted" else {
                "status": "expired", "expires_at": now}
            if block["status"] != "deleted":
                self.repository.update_block(block_id, changes)
            source = self.repository.get_source(block["source_id"])
            if source and source["source_kind"] != "run":
                self.repository.update_source(source["source_id"], {"deleted_at": now})
            pending.extend(b["block_id"] for b in self.repository.blocks({"derived_from_block_ids": block_id}))
        return len(visited)

    def delete_sources(self, **scope_filters: str) -> int:
        """Trusted application cleanup hook, independent of deleted chat/run records."""
        if not scope_filters or not set(scope_filters) <= {"user_id", "room_id", "conversation_id", "message_id", "run_id"}:
            raise ValueError("删除记忆来源需要明确范围")
        count = 0
        for source in self.repository.sources(scope_filters):
            self.repository.update_source(source["source_id"], {"deleted_at": time.time()})
            count += self._invalidate([b["block_id"] for b in self.repository.blocks({"$or": [
                {"source_id": source["source_id"]}, {"evidence_source_ids": source["source_id"]}]})], "deleted")
        return count


def build_long_memory(store, root: str | Path, *,
                      settings_loader: Callable[[str], dict | None] | None = None, runtime=None) -> LongTermMemoryService:
    """Assemble the memory service for the native API or IM bridge."""
    # The processor uses digest/stable_id above; load it after this module is ready.
    from application.services.memory_processing import MemoryProcessingService
    from infra.config import llm_client

    memory = LongTermMemoryService(MongoMemoryRepository(store), MarkdownSourceFiles(root), settings_loader=settings_loader)
    memory.processing = MemoryProcessingService(memory, MemoryProcessingRepository(store, runtime=runtime), JevClassifier(),
        LLMExtractionAdapter(llm_client), input_budget=int(os.getenv("MEMORY_EXTRACTION_INPUT_BUDGET", "12000")))
    return memory
