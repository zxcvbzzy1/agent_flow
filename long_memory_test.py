"""Business-path tests: temporary files and in-memory persistence, no external services."""
from __future__ import annotations

import asyncio
import copy
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from application.services.long_memory import LongTermMemoryService
from application.services.memory_runtime import RunMemoryCoordinator
from application.services.run_state import RunStateService
from domain.memory.long.models import MemoryScope
from domain.memory.long.ports import RoutingDecision
from domain.memory.long.providers import LongTermMemoryProvider
from domain.memory.long.retrieval import BM25Retriever, tokenize
from infra.memory.files import MarkdownSourceFiles
from infra.memory.mongodb import MongoMemoryRepository


def matches(doc, query):
    for key, expected in query.items():
        value = doc.get(key)
        if isinstance(expected, dict):
            if "$ne" in expected and (expected["$ne"] in value if isinstance(value, list) else value == expected["$ne"]):
                return False
            if "$in" in expected and value not in expected["$in"]:
                return False
        elif (expected not in value if isinstance(value, list) else value != expected):
            return False
    return True


class DictStore:
    def __init__(self):
        self.data = {}

    def ensure_index(self, *args, **kwargs):
        pass

    def find_many(self, collection, query=None, sort=None, limit=None, **kwargs):
        result = [copy.deepcopy(doc) for doc in self.data.get(collection, []) if matches(doc, query or {})]
        for key, direction in reversed(sort or []):
            result.sort(key=lambda doc: doc.get(key, 0), reverse=direction < 0)
        return result[:limit] if limit else result

    def find_one(self, collection, query):
        return next(iter(self.find_many(collection, query)), None)

    def insert_one(self, collection, document):
        doc = copy.deepcopy(document)
        doc.setdefault("created_at", time.time())
        doc.setdefault("updated_at", time.time())
        self.data.setdefault(collection, []).append(doc)
        return copy.deepcopy(doc)

    def update_one(self, collection, query, updates, upsert=False):
        return self.update_operators(collection, query, {"$set": {**updates, "updated_at": time.time()}}, upsert=upsert)

    def update_operators(self, collection, query, operators, *, upsert=False):
        doc = next((d for d in self.data.get(collection, []) if matches(d, query)), None)
        if doc is None:
            if not upsert:
                return None
            doc = copy.deepcopy(query)
            doc.update(copy.deepcopy(operators.get("$setOnInsert", {})))
            self.data.setdefault(collection, []).append(doc)
        doc.update(copy.deepcopy(operators.get("$set", {})))
        for key, value in operators.get("$inc", {}).items():
            doc[key] = doc.get(key, 0) + value
        for key, value in operators.get("$addToSet", {}).items():
            if value not in doc.setdefault(key, []):
                doc[key].append(value)
        return copy.deepcopy(doc)

    def delete_many(self, collection, query):
        old = self.data.get(collection, [])
        self.data[collection] = [d for d in old if not matches(d, query)]
        return len(old) - len(self.data[collection])

    delete_one = delete_many


@pytest.fixture
def memory(tmp_path):
    store = DictStore()
    return LongTermMemoryService(MongoMemoryRepository(store), MarkdownSourceFiles(tmp_path / "store"))


def run_record(rid="r1", user="alice", room="room-a", conversation="c1", status="finished", **kwargs):
    return {"run_id": rid, "status": status, "memory_scope": MemoryScope(user, room, conversation).to_dict(),
            "prompt": "composed prompt", "user_question": "Python 记忆检索怎么实现？", "final": "使用 BM25 检索记忆",
            "message_id": "m-" + rid, "finished_at": 1, **kwargs}


def think(rid="r1", eid="e1", agent="a1", text="Python 检索需要保留来源", created=1):
    return {"run_id": rid, "name": "agent.think", "event_id": eid, "execution_id": "execution-" + eid,
            "agent_id": agent, "created_at": created, "payload": {"think": text}}


def blocks(memory, source):
    return memory.repository.blocks({"source_id": source["source_id"]})


def test_archive_original_question_events_lines_and_idempotency(memory):
    record = run_record(user_question="完整问题" * 200)
    events = [think(eid="e2", agent="a2", created=2), think(), think(), think(rid="other"),
              {**think(eid="ignored"), "name": "agent.tool.reasoning"}]
    source = memory.archive_run(record, events)
    text = memory.files.read(source["file_path"])
    assert record["user_question"] in text and "composed prompt" not in text
    result = blocks(memory, source)
    assert [b["event"]["agent_id"] for b in result if b["section_kind"] == "think"] == ["a1", "a2"]
    for block in result:
        assert block["content"] == "\n".join(text.splitlines()[block["start_line"] - 1:block["end_line"]])
        assert block["token_count_method"] == memory.counter.name
    assert source["file_path"] == "alice/room-a/c1/runs/r1.md"
    assert memory.archive_run(record, events)["source_id"] == source["source_id"]
    memory.reindex_source(source["source_id"])
    assert len(blocks(memory, source)) == len(result)


def test_bm25_user_isolation_cross_room_budget_and_usage(memory):
    memory.archive_run(run_record(), [])
    memory.archive_run(run_record("r2", room="room-b", status="failed"), [])
    memory.archive_run(run_record("r3", user="bob"), [])
    scope = MemoryScope("alice", "new-room", "new-conversation")
    result = memory.recall(scope, "Python 记忆", run_id="next")
    assert {b["run_id"] for b in result} == {"r1", "r2"}
    assert "failed" in LongTermMemoryProvider().get({"long_term_memory": result})[0]
    memory.recall(scope, "Python 记忆", run_id="next")
    assert all(b["usage_count"] == 1 for b in memory.repository.blocks({"user_id": "alice"}))
    assert memory.recall(scope, "unrelatedterm", run_id="x") == []
    assert memory.recall(scope, "Python", run_id="x", token_budget=2) == []
    rendered = "\n\n".join(memory.provider.get({"long_term_memory": result}))
    assert memory.counter.count(rendered) <= 4000
    assert "中文" in tokenize("中文，测试") and "文测" not in tokenize("中文，测试")


def test_bm25_ranks_content_and_recency():
    docs = [{"block_id": "a", "content": "apple banana", "updated_at": 1},
            {"block_id": "b", "content": "apple apple apple", "updated_at": 2},
            {"block_id": "c", "content": "orange", "updated_at": 3}]
    assert [b["block_id"] for b in BM25Retriever().rank("apple", docs)] == ["b", "a"]
    docs[1]["content"] = docs[0]["content"]
    assert [b["block_id"] for b in BM25Retriever().rank("apple", docs)] == ["b", "a"]


def test_update_merge_delete_cascade_and_no_resurrection(memory):
    source = memory.archive_run(run_record(), [think()])
    original, thought = blocks(memory, source)
    before = memory.files.read(source["file_path"])
    updated = memory.update_memory("alice", original["block_id"], content="Python 更新后的记忆", tags=["coding"])
    assert updated["memory_id"] == original["memory_id"] and updated["version"] == 2
    merged = memory.aggregate_memories("alice", [updated["block_id"], thought["block_id"]],
        "Python 聚合记忆", MemoryScope("alice", "target", "c2"))
    assert memory.files.read(source["file_path"]) == before
    assert [b["block_id"] for b in memory.recall(MemoryScope("alice"), "Python", run_id="later")] == [merged["block_id"]]
    memory.delete_sources(message_id="m-r1")
    for s in memory.repository.sources({}):
        memory.reindex_source(s["source_id"])
    assert memory.recall(MemoryScope("alice"), "Python", run_id="later2") == []
    assert memory._owned_block("alice", merged["block_id"])["status"] == "deleted"
    assert memory.files.read(source["file_path"]) == before


def test_metadata_expiration_router_and_large_lines(memory):
    source = memory.archive_run(run_record(), [])
    block = blocks(memory, source)[0]
    memory.update_memory("alice", block["block_id"], tags=["topic"], expires_at=time.time() - 1)
    memory.reindex_source(source["source_id"])
    assert memory.recall(MemoryScope("alice"), "Python", run_id="q") == []
    with pytest.raises(KeyError):
        memory.update_memory("bob", block["block_id"], content="no")
    source2 = memory.archive_run(run_record("r2"), [])
    memory.expire_memory("alice", blocks(memory, source2)[0]["block_id"])
    assert memory.recall(MemoryScope("alice"), "Python", run_id="q") == []

    class Router:
        def route(self, source, candidate):
            return RoutingDecision(include=candidate["section_kind"] != "think", memory_type="custom", tags=["routed"])

    memory.router = Router()
    memory.chunker.target_tokens = 10
    source3 = memory.archive_run(run_record("r3", user_question="中" * 100), [think(rid="r3")])
    result = blocks(memory, source3)
    assert all(b["memory_type"] == "custom" and b["section_kind"] != "think" for b in result)
    assert any(b["content"] == "中" * 100 for b in result)


def test_index_retry_preserves_original_and_deleted_blocks(memory, monkeypatch):
    original = memory.repository.put_block
    monkeypatch.setattr(memory.repository, "put_block", lambda doc: (_ for _ in ()).throw(RuntimeError("database failed")))
    with pytest.raises(RuntimeError):
        memory.archive_run(run_record(), [])
    source = memory.repository.sources({})[0]
    assert source["index_status"] == "failed" and memory.files.read(source["file_path"])
    monkeypatch.setattr(memory.repository, "put_block", original)
    assert memory.reindex_source(source["source_id"])["index_status"] == "ready"
    block = blocks(memory, source)[0]
    memory.delete_memory("alice", block["block_id"])
    memory.reindex_source(source["source_id"])
    assert memory._owned_block("alice", block["block_id"])["status"] == "deleted"


async def noop(*args, **kwargs):
    return None


class FakeRuntime:
    def __init__(self):
        self.cache = SimpleNamespace(invalidate=noop, get=self.cache_get)
        self.controls = {}
        self.journal = SimpleNamespace(delete=self.delete_events)

    async def cache_get(self, key, loader):
        return loader()

    async def delete_events(self, *args):
        return 0

    async def remove_projected(self, *args):
        return 0

    async def delete_runtime(self, *args, **kwargs):
        pass

    async def get_state(self, kind, run_id):
        return self.controls.get(run_id)

    async def put_state(self, kind, run_id, changes):
        self.controls.setdefault(run_id, {}).update(changes)

    async def claim(self, kind, run_id, fields):
        await self.put_state(kind, run_id, fields)
        return True

    def track(self, *args):
        pass


class FakeEvents:
    def __init__(self):
        self.runtime = FakeRuntime()
        self.events = []

    async def list_events(self, run_id, **kwargs):
        return [e for e in self.events if e["run_id"] == run_id]

    async def publish(self, run_id, name, payload):
        self.events.append({"run_id": run_id, "name": name, "payload": payload})

    @asynccontextmanager
    async def execution(self, *args, **kwargs):
        yield


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["finished", "failed", "cancelled"])
async def test_terminal_archive_and_missing_identity(memory, status):
    store = memory.repository.store
    events = FakeEvents()
    record = run_record(status=status)
    store.insert_one("runs", record)
    events.events = [think()]
    coordinator = RunMemoryCoordinator(memory, store, events)
    states = RunStateService(store, events.runtime, long_memory=memory, memory_coordinator=coordinator)
    await states.finish_control("r1")
    assert store.find_one("runs", {"run_id": "r1"})["memory_status"] == "ready"
    assert events.runtime.controls["r1"]["status"] == status
    await states.finish_control("r1")
    assert len(memory.repository.sources({})) == 1
    no_identity = run_record("anonymous", memory_scope=None)
    store.insert_one("runs", no_identity)
    assert await coordinator.prepare(no_identity) == []
    await states.finish_control("anonymous")
    assert store.find_one("runs", {"run_id": "anonymous"})["memory_status"] == "skipped"
    await states.delete("r1")
    assert memory.recall(MemoryScope("alice"), "Python", run_id="next") == []


@pytest.mark.asyncio
async def test_archive_failure_does_not_change_run_or_control(memory, monkeypatch):
    store = memory.repository.store
    events = FakeEvents()
    store.insert_one("runs", run_record())
    coordinator = RunMemoryCoordinator(memory, store, events)
    def fail(*args):
        raise OSError("disk unavailable")
    monkeypatch.setattr(memory, "archive_run", fail)
    await RunStateService(store, events.runtime, memory_coordinator=coordinator).finish_control("r1")
    row = store.find_one("runs", {"run_id": "r1"})
    assert row["status"] == "finished" and row["memory_status"] == "failed"
    assert events.runtime.controls["r1"]["status"] == "finished"


def test_provider_and_context_compatibility(memory):
    from application.services.contexts import ContextService
    from domain.agent.plan.orchestrator import OrchestratorState
    service = ContextService.__new__(ContextService)
    config = {"provider_config": [{"provider_id": "user_prompt"}]}
    engine = service._build_engine(config)
    assert any(isinstance(p, LongTermMemoryProvider) for p in engine._providers)
    source = memory.archive_run(run_record(), [])
    state = OrchestratorState(long_term_memory=blocks(memory, source)).to_context_dict()
    assert "长期记忆" in engine.build(state)
    assert LongTermMemoryProvider().get({}) == []
    config["provider_config"].append({"provider_id": "long_term_memory", "enabled": False})
    assert "长期记忆" not in service._build_engine(config).build(state)
    restored = service._build_provider(service._provider_to_config(LongTermMemoryProvider()), engine.get_memory())
    assert isinstance(restored, LongTermMemoryProvider)


def run_harness(memory, mode="react"):
    from application.services.runs import RunOrchestrationService
    from application.services.contexts import ContextService
    from domain.agent_base import AgentBase
    from domain.agent.plan.planAgent import PlanAgent
    from domain.context.context import ContextEngine
    from domain.context.providers import UserPromptProvider
    from domain.memory.short.default_short_term_memory import DefaultShortTermMemory

    events = FakeEvents()
    store = memory.repository.store
    captured = []

    class LLM:
        async def chat(self, messages):
            captured.append(messages)
            assert "长期记忆" in messages[-1]["content"]
            return '{"think":"Python 历史已加载", "tool_calls":[], "is_finished":true, "final":"完成"}'

    contexts = ContextService.__new__(ContextService)
    def engine(kind="executor"):
        return contexts._build_engine({"kind": kind, "provider_config": [{"provider_id": "user_prompt"}]})

    agents = {}
    def build(agent_id):
        if agent_id == "planner":
            from domain.state import Plan
            agent = PlanAgent(agent_id, agent_id, LLM(), engine("planner"))
            async def generate(state, executor_ids):
                assert state["long_term_memory"]
                assert "长期记忆" in agent.context_engine.build(state)
                plan = Plan()
                plan.add_steps([{"step_id": "s1", "executor_id": "executor", "title": "Python", "instruction": "Python"}])
                return plan
            async def replan(plan, state):
                assert state["long_term_memory"]
                return {"action": "continue"}
            async def summarize(state):
                assert state["long_term_memory"]
                return "计划完成"
            agent.generate_plan, agent.replan_after_observation, agent.summarize_result = generate, replan, summarize
        else:
            agent = AgentBase(agent_id, agent_id, LLM(), engine())
            agent.skill_recall_enabled = False
        agents[agent_id] = agent
        return agent

    factory = SimpleNamespace(build_run_agent=build, get_agent_record=lambda aid: {
        "agent_type": "planner" if aid == "planner" else "executor"})
    step_engine = ContextEngine([UserPromptProvider()], DefaultShortTermMemory(["agent_history", "tool_respond", "error", "skill"]))
    frontend = SimpleNamespace(register_agent_run=lambda *a: None, unregister_agent_run=lambda *a: None)
    runs = RunOrchestrationService(store, factory, SimpleNamespace(get_engine=lambda cid: step_engine), events, frontend,
                                   long_memory=memory)
    return runs, events, agents, captured


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["react", "plan"])
async def test_orchestration_paths_prepare_once_and_archive(memory, mode, monkeypatch):
    memory.archive_run(run_record("history"), [])
    runs, events, agents, captured = run_harness(memory, mode)
    calls = []
    original = memory.recall
    def recall(*args, **kwargs):
        calls.append(kwargs["run_id"])
        return original(*args, **kwargs)
    monkeypatch.setattr(memory, "recall", recall)
    record = await runs.create_run("Python", mode=mode, executor_agent_id="executor", executor_agent_ids=["executor"],
        planner_agent_id="planner", auto_start=False, memory_scope=MemoryScope("alice", "other", "next"))
    events.events = [think(rid=record["run_id"])]
    await runs._execute_run(record)
    result = memory.repository.store.find_one("runs", {"run_id": record["run_id"]})
    assert result["status"] == "finished", result.get("error")
    assert result["memory_status"] == "ready" and calls == [record["run_id"]]
    assert captured and agents["executor"].states["long_term_memory"]
    if mode == "plan":
        assert agents["planner"].states["long_term_memory"] == agents["executor"].states["long_term_memory"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "cancelled"])
async def test_orchestration_failure_and_cancellation_are_archived(memory, status, monkeypatch):
    runs, events, _, _ = run_harness(memory)
    record = await runs.create_run("Python", mode="react", executor_agent_id="executor", auto_start=False,
        memory_scope=MemoryScope("alice", "other", "next"))
    async def stop(record):
        if status == "cancelled":
            raise asyncio.CancelledError()
        raise RuntimeError("expected failure")
    monkeypatch.setattr(runs, "_execute_react_run", stop)
    if status == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await runs._execute_run(record)
    else:
        await runs._execute_run(record)
    stored = memory.repository.store.find_one("runs", {"run_id": record["run_id"]})
    assert stored["status"] == status and stored["memory_status"] == "ready"


@pytest.mark.asyncio
async def test_dm_uses_login_identity_full_question_and_cleanup(memory):
    from im_backend.application.services.messaging.conversations import ConversationService
    from im_backend.application.services.platform.cleanup import IMCleanupService
    memory.archive_run(run_record("history"), [])
    store = memory.repository.store
    runs, events, _, captured = run_harness(memory)
    bridge = SimpleNamespace(runs=runs, events=events, agents=runs._agents, runtime=events.runtime,
        long_memory=memory, list_run_events=events.list_events)
    service = ConversationService.__new__(ConversationService)
    service._store, service._bridge, service._events, service.runtime = store, bridge, events, events.runtime
    service._reply_tasks = {}
    service._runtime_profile = lambda *a: None
    service._rebuild_agent_history = lambda *a, **kw: None
    service._apply_agent_workdir = lambda *a: None
    service._apply_pinned_context = lambda *a: None
    service._compose_prompt = lambda message: "Python composed"
    async def add_reply(**kwargs):
        return {"message_id": "reply", **kwargs}
    service.add_conversation_message = add_reply
    store.insert_one("im_conversations", {"conversation_id": "dm", "agent_id": "executor"})
    question = "Python 完整问题" * 100
    message = {"message_id": "dm-msg", "conversation_id": "dm", "sender_type": "user",
               "sender_id": "bob", "content_parts": [{"type": "text", "text": question}]}
    store.insert_one("im_messages", message)
    started = await service.reply_to_conversation_message(conversation_id="dm", message_id="dm-msg", user_id="alice")
    task = service._reply_tasks[started["run_id"]]
    await task
    record = store.find_one("runs", {"run_id": started["run_id"]})
    assert record["memory_scope"]["user_id"] == "alice"
    assert record["user_question"] == question and len(record["prompt"]) == 200
    assert record["status"] == "finished" and record["memory_status"] == "ready"
    assert captured
    source = memory.repository.get_source(record["memory_source_id"])
    assert source["file_path"].startswith("alice/direct/dm/")
    assert question in memory.files.read(source["file_path"])
    cleanup = IMCleanupService(store, events.runtime, long_memory=memory)
    await cleanup.delete_conversation("dm")
    assert memory.repository.get_source(source["source_id"])["deleted_at"] is not None


@pytest.mark.asyncio
async def test_group_dispatch_passes_login_not_message_author(memory):
    from im_backend.application.services.orchestration.runs import GroupRunService
    service = GroupRunService.__new__(GroupRunService)
    received = {}
    async def create_run(**kwargs):
        received.update(kwargs)
        return kwargs
    async def add_runtime_message(**kwargs):
        return {"message_id": "mirror-message"}
    service._agents = SimpleNamespace(ensure_agent_access=lambda *a: None, ensure_context_access=lambda *a: None)
    service._bridge = SimpleNamespace(create_runtime_conversation=lambda **kw: {"conversation_id": "mirror"},
        add_runtime_message=add_runtime_message, create_run=create_run)
    service._messages = SimpleNamespace(message_text=lambda message: message["text"])
    service._favorites = SimpleNamespace(context_items=lambda *a: [])
    service._room_history_before = lambda *a: []
    await service._create_agent_flow_run(room={"room_id": "room"},
        message={"message_id": "m", "sender_id": "bob", "text": "original"}, prompt="with references",
        mode="plan", user_id="alice", conversation_id="conversation")
    assert received["memory_scope"] == MemoryScope("alice", "room", "conversation")
    assert received["user_question"] == "original"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["message", "room"])
async def test_chat_cleanup_disables_cross_room_derivatives(memory, kind):
    from im_backend.application.services.platform.cleanup import IMCleanupService
    store = memory.repository.store
    source = memory.archive_run(run_record(), [think()])
    merged = memory.aggregate_memories("alice", [b["block_id"] for b in blocks(memory, source)],
        "Python 聚合", MemoryScope("alice", "another-room", "another-session"))
    message = {"message_id": "m-r1", "run_id": "r1", "sender_type": "user", "room_id": "room-a"}
    store.insert_one("im_messages", message)
    cleanup = IMCleanupService(store, long_memory=memory)
    if kind == "message":
        await cleanup.delete_message(message)
    else:
        await cleanup.delete_room("room-a")
    assert memory._owned_block("alice", merged["block_id"])["status"] == "deleted"
    assert memory.recall(MemoryScope("alice"), "Python", run_id="next") == []


@pytest.mark.asyncio
async def test_disabled_provider_does_not_count_usage(memory):
    source = memory.archive_run(run_record("history"), [])
    store = memory.repository.store
    record = run_record()
    store.insert_one("runs", record)
    coordinator = RunMemoryCoordinator(memory, store, FakeEvents())
    selected = await coordinator.prepare(record)
    agent = SimpleNamespace(states={}, context_engine=SimpleNamespace(_providers=[]))
    await coordinator.inject(agent, selected, record["run_id"])
    assert blocks(memory, source)[0]["usage_count"] == 0
    agent.context_engine._providers = [LongTermMemoryProvider()]
    await coordinator.inject(agent, selected, record["run_id"])
    await coordinator.inject(agent, selected, record["run_id"])
    assert blocks(memory, source)[0]["usage_count"] == 1


@pytest.mark.asyncio
async def test_finished_orphan_completes_missing_archive(memory):
    runs, events, _, _ = run_harness(memory)
    record = run_record()
    memory.repository.store.insert_one("runs", record)
    await runs.handle_orphan({"kind": "orchestration", "run_id": "r1"})
    assert memory.repository.store.find_one("runs", {"run_id": "r1"})["memory_status"] == "ready"
