"""Restartable user batches with a classification barrier and atomic publication marker."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid

from application.services.long_memory import digest, stable_id
from domain.memory.long.models import MemoryBlock
from domain.memory.long.processing import (CATEGORIES, ChoiceResult, MemoryCandidate, MemoryClassifier, MemoryExtractor,
                                           NoulResult, ProcessingRepository, ProcessingLeaseLost, validate_memories)
from domain.memory.long.prompts import MERGE_VERSION

logger = logging.getLogger(__name__)


class MemoryProcessingService:
    def __init__(self, memory, repository: ProcessingRepository, classifier: MemoryClassifier,
                 extractor: MemoryExtractor, *, input_budget=12000, concurrency=4, interval=30):
        self.memory, self.repository = memory, repository
        self.classifier, self.extractor = classifier, extractor
        self.input_budget, self.concurrency, self.interval = input_budget, concurrency, interval
        self._task = None
        self._tick_lock = asyncio.Lock()

    def readiness(self):
        return {"jev_ready": self.classifier.ready(), "extraction_ready": self.extractor.ready(),
                "jev_model": self.classifier.model, "extraction_model": self.extractor.model}

    async def start(self):
        if self._task is None or self._task.done():
            await asyncio.to_thread(self.repository.start)
            self._task = asyncio.create_task(self._loop(), name="long-memory-processing")

    async def close(self):
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await asyncio.to_thread(self.repository.close)

    async def _loop(self):
        while True:
            try:
                await self.tick()
            except Exception:
                logger.exception("长期记忆后台扫描失败；下次扫描恢复")
            await asyncio.sleep(self.interval)

    async def tick(self, *, now=None):
        async with self._tick_lock:
            now = time.time() if now is None else now
            sources = await asyncio.to_thread(self.memory.repository.sources, {"index_status": "queued"})
            batches = await asyncio.to_thread(self.repository.batches,
                {"status": {"$in": ["waiting", "pending", "classifying", "extracting", "publishing", "finalizing"]}})
            legacy_users = await asyncio.to_thread(self.repository.legacy_users)
            users = sorted({s["user_id"] for s in sources} | {b["user_id"] for b in batches} | legacy_users)
            # Separate user workers prevent one slow model request blocking everyone.
            semaphore = asyncio.Semaphore(self.concurrency)

            async def process(user):
                async with semaphore:
                    await self._user(user, now)
            results = await asyncio.gather(*(process(user) for user in users), return_exceptions=True)
            for user, result in zip(users, results):
                if isinstance(result, Exception):
                    logger.error("长期记忆用户 %s 处理暂时中断：%s", user, type(result).__name__)

    async def _user(self, user, now):
        owner = str(uuid.uuid4())
        if not await asyncio.to_thread(self.repository.acquire, user, owner):
            return
        worker = asyncio.current_task()
        lease_lost = False
        async def heartbeat():
            nonlocal lease_lost
            while True:
                await asyncio.sleep(15)
                try:
                    valid = await asyncio.to_thread(self.repository.renew, user, owner)
                except Exception:
                    valid = False
                if not valid:
                    lease_lost = True
                    worker.cancel()
                    return
        pulse = asyncio.create_task(heartbeat())
        try:
            batches = await asyncio.to_thread(self._form_batches, user, owner, now)
            for batch in batches:
                if batch["status"] in {"completed", "failed", "waiting"}:
                    continue
                await self._process(batch, owner)
        except asyncio.CancelledError:
            if not lease_lost:
                raise
        finally:
            pulse.cancel()
            try:
                with contextlib.suppress(asyncio.CancelledError):
                    await pulse
            finally:
                try:
                    await asyncio.to_thread(self.repository.release, user, owner)
                except Exception:
                    logger.warning("长期记忆租约释放失败，将由 Redis TTL 回收")

    def _form_batches(self, user, owner, now):
        self.repository.migrate_legacy(user, owner)
        existing = self.repository.batches({"user_id": user})
        assigned = {sid for b in existing for sid in b["source_ids"]}
        queued = sorted(self.memory.repository.sources({"user_id": user, "index_status": "queued"}),
                        key=lambda s: (s["created_at"], s["source_id"]))
        # A crash after the terminal marker but before updating source readiness
        # must not leave successfully archived memories permanently unavailable.
        completed_sources = {sid for b in existing if b["status"] == "completed" for sid in b["source_ids"]}
        for source in queued:
            if source["source_id"] in completed_sources:
                self.memory.repository.update_source(source["source_id"], {"index_status": "ready", "error": ""})
        free = [s for s in queued if s["source_id"] not in assigned]
        waiting = [b for b in existing if b["status"] == "waiting"]
        for batch in waiting:
            self.repository.claim_batch(batch["batch_id"], owner)
            extra = [s for s in free if s["processing_revision"] == batch["config_revision"]]
            extra = extra[:batch["config"]["run_batch_size"] - len(batch["source_ids"])]
            batch["source_ids"].extend(s["source_id"] for s in extra)
            free = [s for s in free if s not in extra]
            self.repository.update_batch(batch["batch_id"], owner, {"source_ids": batch["source_ids"],
                "run_count": len(batch["source_ids"])})
        while free:
            first = free[0]
            group = [s for s in free if s["processing_revision"] == first["processing_revision"]]
            group = group[:first["processing_config"]["run_batch_size"]]
            batch = {"batch_id": stable_id("batch:" + first["source_id"]), "user_id": user,
                "config": first["processing_config"], "config_revision": first["processing_revision"],
                "source_ids": [s["source_id"] for s in group], "status": "waiting", "stage": "waiting",
                "run_count": len(group), "candidate_count": 0, "classified_count": 0, "accepted_count": 0,
                "output_count": 0, "category_progress": {}, "steps": {}, "error": "",
                "created_at": first["created_at"], "updated_at": now, "owner": owner, "schema_version": 1}
            self.repository.put_batch(batch)
            waiting.append(batch)
            free = [s for s in free if s not in group]
        for batch in waiting:
            if len(batch["source_ids"]) >= batch["config"]["run_batch_size"] or now - batch["created_at"] >= 86400:
                self.repository.update_batch(batch["batch_id"], owner, {"status": "pending", "started_at": now})
        batches = self.repository.batches({"user_id": user})
        for batch in batches:
            if batch["status"] not in {"failed", "completed", "waiting"}:
                self.repository.claim_batch(batch["batch_id"], owner)
        return batches

    async def _save(self, batch, owner, changes):
        if not await asyncio.to_thread(self.repository.renew, batch["user_id"], owner):
            raise ProcessingLeaseLost("用户处理租约已失效")
        await asyncio.to_thread(self.repository.update_batch, batch["batch_id"], owner, changes)
        batch.update(changes)

    async def _retry_model(self, call):
        for attempt in range(3):
            try:
                return await call()
            except Exception:
                if attempt == 2:
                    raise
                await asyncio.sleep(.25 * 2 ** attempt)

    def _candidates(self, batch, owner):
        for sid in batch["source_ids"]:
            source = self.memory.repository.get_source(sid)
            if not source or source["user_id"] != batch["user_id"]:
                raise ValueError("批次来源不存在或归属不符")
            text = self.memory.files.read(source["file_path"])
            if digest(text) != source["content_hash"]:
                raise ValueError("L3 原文校验失败")
            index = 0
            for section in source["sections"]:
                for chunk in self.memory.chunker.split(text, section):
                    candidate = {**chunk, **{key: source[key] for key in (
                        "source_id", "user_id", "room_id", "conversation_id", "run_id", "run_status",
                        "message_id", "file_path", "created_at")},
                        "candidate_id": stable_id(f"candidate:{sid}:{index}"), "batch_id": batch["batch_id"],
                        "status": "pending"}
                    self.repository.put_candidate(MemoryCandidate.model_validate(candidate).model_dump(), owner)
                    index += 1
        return self.repository.candidates(batch["batch_id"])

    async def _classify(self, batch, owner, candidates):
        await self._save(batch, owner, {"status": "classifying", "stage": "classification",
                                       "candidate_count": len(candidates)})
        semaphore = asyncio.Semaphore(self.concurrency)

        async def classify(candidate):
            async with semaphore:
                if candidate["status"] in {"accepted", "rejected"}:
                    return
                if "noul_result" not in candidate:
                    async def gate():
                        return NoulResult.model_validate(await self.classifier.noul(candidate)).model_dump()
                    candidate["noul_result"] = await self._retry_model(gate)
                    await asyncio.to_thread(self.repository.update_candidate, candidate["candidate_id"],
                        {"noul_result": candidate["noul_result"]}, batch_id=batch["batch_id"], owner=owner)
                if candidate["noul_result"]["noul"] < batch["config"]["noul_threshold"]:
                    changes = {"status": "rejected"}
                else:
                    async def choose():
                        return ChoiceResult.model_validate(await self.classifier.choice(candidate)).validated()
                    choice = await self._retry_model(choose)
                    changes = {"status": "accepted", "choice_result": choice}
                await asyncio.to_thread(self.repository.update_candidate, candidate["candidate_id"], changes,
                                       batch_id=batch["batch_id"], owner=owner)
                candidate.update(changes)
        # return_exceptions waits for every in-flight classification before failure/retry.
        results = await asyncio.gather(*(classify(c) for c in candidates), return_exceptions=True)
        await self._save(batch, owner, {"classified_count": sum(c["status"] != "pending" for c in candidates),
            "accepted_count": sum(c["status"] == "accepted" for c in candidates)})
        for result in results:
            if isinstance(result, BaseException):
                raise result
        if any(c["status"] not in {"accepted", "rejected"} for c in candidates):
            raise ValueError("分类尚未全部完成")

    def _pack(self, candidates):
        groups, group, size = [], [], 0
        for candidate in candidates:
            count = self.memory.counter.count(json.dumps(candidate, ensure_ascii=False))
            if count > self.input_budget:
                raise ValueError("单个完整候选超过 MEMORY_EXTRACTION_INPUT_BUDGET；请提高服务端预算后重试")
            if group and size + count > self.input_budget:
                groups.append(group)
                group, size = [], 0
            group.append(candidate)
            size += count
        if group:
            groups.append(group)
        return groups

    async def _extract(self, batch, owner, candidates):
        await self._save(batch, owner, {"status": "extracting", "stage": "extraction"})
        all_results = []
        for category in CATEGORIES:
            selected = [c for c in candidates if c["status"] == "accepted" and c["choice_result"]["category"] == category]
            manifests = batch.setdefault("extraction_groups", {})
            if category not in manifests:
                manifests[category] = [[c["candidate_id"] for c in group] for group in self._pack(selected)]
                await self._save(batch, owner, {"extraction_groups": manifests})
            by_id = {c["candidate_id"]: c for c in selected}
            groups = [[by_id[cid] for cid in group] for group in manifests[category]]
            progress = {"total": len(groups), "completed": 0, "status": "extracting" if groups else "completed"}
            batch["category_progress"][category] = progress
            await self._save(batch, owner, {"category_progress": batch["category_progress"]})
            rows, models = [], []
            for index, group in enumerate(groups):
                key = f"extract:{category}:{index}"
                if key not in batch["steps"]:
                    async def extract():
                        response = await self.extractor.extract(category, group)
                        response["memories"] = validate_memories(response["memories"], category, group)
                        return response
                    batch["steps"][key] = await self._retry_model(extract)
                    await self._save(batch, owner, {"steps": batch["steps"]})
                response = batch["steps"][key]
                rows.extend(validate_memories(response["memories"], category, group))
                models.append({"model": response["model"], "prompt_version": response["prompt_version"]})
                progress["completed"] = index + 1
                await self._save(batch, owner, {"category_progress": batch["category_progress"]})
            merged = []
            for row in rows:
                for previous in merged:
                    pair = [{k: v for k, v in item.items() if k != "evidence_candidate_ids"} for item in (previous, row)]
                    key = "merge:" + digest(json.dumps([category, pair], sort_keys=True, ensure_ascii=False))
                    if key not in batch["steps"]:
                        exact = pair[0] == pair[1]
                        if not exact and self.memory.counter.count(json.dumps(pair, ensure_ascii=False)) > self.input_budget:
                            raise ValueError("归并输入超出提取预算，请提高服务端预算后重试")
                        batch["steps"][key] = exact or await self._retry_model(
                            lambda: self.extractor.equivalent(category, previous, row))
                        await self._save(batch, owner, {"steps": batch["steps"]})
                    if batch["steps"][key]:
                        previous["evidence_candidate_ids"] = list(dict.fromkeys(
                            previous["evidence_candidate_ids"] + row["evidence_candidate_ids"]))
                        previous["tags"] = list(dict.fromkeys(previous["tags"] + row["tags"]))
                        break
                else:
                    merged.append(row)
            progress["status"] = "completed"
            progress["output_count"] = len(merged)
            await self._save(batch, owner, {"category_progress": batch["category_progress"]})
            all_results.extend({**row, "category": category, "extraction_models": models} for row in merged)
        return all_results

    def _build_blocks(self, batch, candidates, results):
        by_id = {c["candidate_id"]: c for c in candidates}
        blocks = []
        for index, row in enumerate(results):
            evidence = [by_id[cid] for cid in row["evidence_candidate_ids"]]
            refs = [{k: c[k] for k in ("candidate_id", "source_id", "file_path", "start_line", "end_line",
                "run_id", "run_status", "room_id", "conversation_id", "message_id", "section_kind", "event")}
                for c in evidence]
            first = refs[0]
            bid = stable_id(f"extracted:{batch['batch_id']}:{index}")
            block = MemoryBlock(block_id=bid, memory_id=bid, user_id=batch["user_id"],
                **{k: first[k] for k in ("source_id", "file_path", "start_line", "end_line", "run_id",
                                        "run_status", "room_id", "conversation_id", "message_id", "event")},
                content=row["content"], content_hash=digest(row["content"]), section_kind="extracted",
                memory_type=row["category"], tags=row["tags"], token_count=self.memory.counter.count(row["content"]),
                token_count_method=self.memory.counter.name, batch_id=batch["batch_id"], evidence_refs=refs,
                evidence_source_ids=list(dict.fromkeys(r["source_id"] for r in refs)),
                metadata={"structure": row["structure"], "extraction_models": row["extraction_models"],
                    "merge_prompt_version": MERGE_VERSION, "classifications": [{"candidate_id": c["candidate_id"],
                        "noul": c["noul_result"], "choice": c["choice_result"]} for c in evidence]}).to_dict()
            # Deletion is also checked at read time across ALL evidence sources.
            if any(self.memory.repository.get_source(r["source_id"]).get("deleted_at") is not None for r in refs):
                block.update(status="deleted", deleted_at=time.time())
            blocks.append(block)
        return blocks

    async def _process(self, batch, owner):
        if batch["status"] == "finalizing":
            await asyncio.to_thread(self.repository.finalize, batch["batch_id"], owner, self.memory.repository)
            return
        try:
            await self._save(batch, owner, {"status": "classifying", "stage": "classification"})
            candidates = await asyncio.to_thread(self._candidates, batch, owner)
            await self._classify(batch, owner, candidates)
            # No extraction (including retries/merging) is reachable before the barrier above.
            results = await self._extract(batch, owner, candidates)
            blocks = await asyncio.to_thread(self._build_blocks, batch, candidates, results)
            await self._save(batch, owner, {"status": "finalizing", "stage": "publication", "outcome": "completed",
                "publication_blocks": blocks, "error": "", "output_count": len(results), "completed_at": time.time()})
        except ProcessingLeaseLost:
            return
        except Exception as exc:
            # SDK exception strings may include response bodies. Expose only safe diagnostics.
            error = str(exc)[:500] if isinstance(exc, ValueError) else f"{type(exc).__name__}；请检查服务端模型连接后重试"
            await self._save(batch, owner, {"status": "finalizing", "outcome": "failed", "error": error,
                "failed_stage": batch["stage"], "failed_at": time.time(), "publication_blocks": []})
        # Storage failures leave the frozen outbox pending; never turn them into a
        # model failure or rerun extraction. The next tick resumes acknowledgement.
        await asyncio.to_thread(self.repository.finalize, batch["batch_id"], owner, self.memory.repository)
