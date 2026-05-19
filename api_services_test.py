from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from api.core.dependencies import get_container
from api.index import app
from domain.agent_base import AgentBase, ToolCall
from domain.event import Event
from domain.runtime_hooks import (
    get_human_approval_provider,
    get_run_context_provider,
    register_human_approval_provider,
    register_run_context_provider,
)
from application.services.llm_streaming import StreamingObservableLLMClient
from infra.tool.common_func import HumanCollaborationAuditor, human_approval_service


class _HumanConfirmSpec:
    tool_name = "bash"
    tool_metadata = {"require_human_confirm": True}


class _HumanConfirmFactory:
    def get_spec(self, event_name: str):
        return _HumanConfirmSpec()


class _RecordingHumanBus:
    def __init__(self) -> None:
        self.events = []

    async def publish_one(self, event: Event):
        self.events.append(event)
        return Event("human.bash.confirmed", {"approved": True, "reason": "terminal ok"})


class _HumanInputBus:
    def __init__(self) -> None:
        self.events = []

    async def publish_one(self, event: Event):
        self.events.append(event)
        answer = (await human_approval_service.input("confirm?")).strip().lower()
        approved = answer in {"yes", "y"}
        return Event(
            "human.bash.confirmed",
            {
                "approved": approved,
                "reason": "input approved" if approved else f"input rejected: {answer}",
            },
        )


class _RecordingApprovalProvider:
    def __init__(self) -> None:
        self.requests = []

    async def request_approval(self, **kwargs):
        self.requests.append(kwargs)
        return {"approved": True, "reason": "web ok"}


class _StaticRunContextProvider:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id

    def run_id_for_agent(self, agent_id: str) -> str:
        return self.run_id


class _FakeToolHandle:
    def __init__(self) -> None:
        self.payload = None

    async def emit_called(self, payload):
        self.payload = payload


class _FakeToolFactory:
    def __init__(self, handle: _FakeToolHandle) -> None:
        self.handle = handle

    def tool(self, tool_name: str):
        return self.handle


class _StreamingFakeLLM:
    model = "fake-model"
    max_tokens = 1024

    def __init__(self, chunks: list[str]) -> None:
        self.chunks = chunks

    async def stream_chat(self, messages, model=None):
        for chunk in self.chunks:
            yield chunk


def test_health_and_cors_headers():
    client = TestClient(app)

    response = client.options(
        "/api/tools",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"


def test_tools_api_lists_builtin_and_uploaded_tool():
    client = TestClient(app)

    tools = client.get("/api/tools").json()["items"]
    assert any(item["name"] == "bash" for item in tools)

    response = client.post(
        "/api/tools/upload",
        json={
            "name": "unit_test_tool",
            "description": "test upload",
            "field": "system",
            "input_schema": {"type": "object", "properties": {}},
            "metadata": {"unit": True},
            "source_code": "",
        },
    )

    assert response.status_code == 200
    assert response.json()["item"]["name"] == "unit_test_tool"
    tools = client.get("/api/tools").json()["items"]
    assert any(item["name"] == "unit_test_tool" for item in tools)


def test_context_agent_run_and_conversation_apis():
    client = TestClient(app)

    context = client.post(
        "/api/contexts",
        json={
            "name": "API Test Context",
            "kind": "executor",
            "strategy_config": {"type": "full_history", "keep_last": 2},
            "available_fields": ["system"],
        },
    ).json()["item"]
    assert client.get(f"/api/contexts/{context['context_id']}").status_code == 200

    agent = client.post(
        "/api/agents",
        json={
            "name": "API Test Agent",
            "agent_type": "executor",
            "context_id": context["context_id"],
        },
    ).json()["item"]
    assert agent["agent_type"] == "executor"

    run = client.post(
        "/api/runs",
        json={
            "prompt": "测试任务",
            "planner_agent_id": "default_planner",
            "executor_agent_ids": ["default_executor"],
            "context_id": "default_step",
            "auto_start": False,
        },
    ).json()["item"]
    assert client.get(f"/api/runs/{run['run_id']}").json()["item"]["status"] == "pending"

    conversation = client.post(
        "/api/conversations",
        json={"title": "API Test Conversation"},
    ).json()["item"]
    message = client.post(
        f"/api/conversations/{conversation['conversation_id']}/messages",
        json={"role": "user", "content": "你好"},
    ).json()["item"]
    queue_item = client.post(
        f"/api/conversations/{conversation['conversation_id']}/queue",
        json={"message_id": message["message_id"]},
    ).json()["item"]

    assert queue_item["status"] == "pending"
    queue = client.get(f"/api/conversations/{conversation['conversation_id']}/queue").json()["items"]
    assert queue[-1]["message_id"] == message["message_id"]


def test_event_stream_service_formats_historical_finished_event():
    container = get_container()
    run_id = "stream-test-run"
    container.events.publish(run_id, "workflow.started", {"ok": True})
    container.events.publish(run_id, "workflow.finished", {"final": "done"})

    events = container.events.list_events(run_id)

    assert [event["name"] for event in events] == ["workflow.started", "workflow.finished"]
    assert "event: workflow.finished" in container.events.format_sse(events[-1])


def test_frontend_bridge_mirrors_tool_events_to_run_stream():
    container = get_container()
    run_id = "bridge-test-run"

    container.frontend_bridge.register_agent_run("bridge_agent", run_id)
    container.frontend_bridge.mirror_tool_event(
        Event(
            name="infra.system.bash.called",
            payload={"agent_id": "bridge_agent", "run_id": run_id, "command": "echo hi"},
        )
    )
    container.frontend_bridge.mirror_tool_event(
        Event(
            name="infra.system.bash.failed",
            payload={"agent_id": "bridge_agent", "name": "bash", "success": False, "respond": "no"},
        )
    )

    events = container.events.list_events(run_id)

    assert [event["name"] for event in events[-2:]] == ["tool.called", "tool.failed"]
    assert events[-1]["payload"]["respond"] == "no"


@pytest.mark.asyncio
async def test_human_confirmation_can_be_requested_and_resolved():
    container = get_container()
    run_id = "confirm-test-run"

    task = asyncio.create_task(
        container.human_confirmations.request_confirmation(
            run_id=run_id,
            agent_id="agent_001",
            tool_name="bash",
            called_event_name="infra.system.bash.called",
            arguments={"command": "echo hi"},
        )
    )
    await asyncio.sleep(0)

    pending = container.human_confirmations.list_pending(run_id)
    assert len(pending) == 1
    assert pending[0]["status"] == "pending"

    resolved = container.human_confirmations.resolve(
        run_id=run_id,
        confirmation_id=pending[0]["confirmation_id"],
        approved=True,
        reason="ok",
    )
    result = await task
    events = container.events.list_events(run_id)

    assert result == {"approved": True, "reason": "ok"}
    assert resolved["approved"] is True
    assert [event["name"] for event in events[-2:]] == [
        "human.confirmation.requested",
        "human.confirmation.resolved",
    ]


@pytest.mark.asyncio
async def test_confirmation_api_lists_and_resolves_pending_request():
    container = get_container()
    run_id = "confirm-api-run"

    task = asyncio.create_task(
        container.human_confirmations.request_confirmation(
            run_id=run_id,
            agent_id="agent_001",
            tool_name="bash",
            called_event_name="infra.system.bash.called",
            arguments={"command": "pwd"},
        )
    )
    await asyncio.sleep(0)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        pending_response = await client.get(f"/api/runs/{run_id}/confirmations")
        pending = pending_response.json()["items"]

        assert pending_response.status_code == 200
        assert len(pending) == 1

        resolve_response = await client.post(
            f"/api/runs/{run_id}/confirmations/{pending[0]['confirmation_id']}",
            json={"approved": False, "reason": "deny"},
        )
    result = await task

    assert resolve_response.status_code == 200
    assert resolve_response.json()["item"]["approved"] is False
    assert result == {"approved": False, "reason": "deny"}


@pytest.mark.asyncio
async def test_human_collaboration_uses_web_confirmation_when_run_context_exists(monkeypatch):
    monkeypatch.delenv("AGENT_FLOW_AUTO_CONFIRM", raising=False)
    previous_provider = get_human_approval_provider()
    previous_run_context = get_run_context_provider()
    previous_input_func = human_approval_service.input_func
    provider = _RecordingApprovalProvider()
    run_context = _StaticRunContextProvider("run-from-context")
    bus = _HumanInputBus()
    try:
        register_human_approval_provider(provider)
        register_run_context_provider(run_context)
        auditor = HumanCollaborationAuditor(_HumanConfirmFactory(), bus)

        result = await auditor.audit(
            Event(
                "infra.system.bash.called",
                {"agent_id": "agent-web", "command": "pwd"},
            )
        )

        assert result.approved is True
        assert result.reason == "input approved"
        assert bus.events[0].name == "human.bash"
        assert provider.requests == [
            {
                "run_id": "run-from-context",
                "agent_id": "agent-web",
                "tool_name": "bash",
                "called_event_name": "infra.system.bash.called",
                "arguments": {"agent_id": "agent-web", "command": "pwd"},
            }
        ]
    finally:
        register_human_approval_provider(previous_provider)
        register_run_context_provider(previous_run_context)
        human_approval_service.input_func = previous_input_func


@pytest.mark.asyncio
async def test_human_collaboration_falls_back_to_human_event_without_run_context(monkeypatch):
    monkeypatch.delenv("AGENT_FLOW_AUTO_CONFIRM", raising=False)
    previous_provider = get_human_approval_provider()
    previous_run_context = get_run_context_provider()
    provider = _RecordingApprovalProvider()
    bus = _RecordingHumanBus()
    try:
        register_human_approval_provider(provider)
        register_run_context_provider(_StaticRunContextProvider(""))
        auditor = HumanCollaborationAuditor(_HumanConfirmFactory(), bus)

        result = await auditor.audit(
            Event(
                "infra.system.bash.called",
                {"agent_id": "agent-terminal", "command": "pwd"},
            )
        )

        assert result.approved is True
        assert result.reason == "terminal ok"
        assert provider.requests == []
        assert bus.events[0].name == "human.bash"
    finally:
        register_human_approval_provider(previous_provider)
        register_run_context_provider(previous_run_context)


@pytest.mark.asyncio
async def test_human_approval_service_input_func_can_replace_confirmation_policy(monkeypatch):
    monkeypatch.delenv("AGENT_FLOW_AUTO_CONFIRM", raising=False)
    previous_input_func = human_approval_service.input_func
    bus = _HumanInputBus()

    async def deny_all_policy(prompt):
        return "no custom deny"

    try:
        human_approval_service.input_func = deny_all_policy
        auditor = HumanCollaborationAuditor(_HumanConfirmFactory(), bus)

        result = await auditor.audit(
            Event(
                "infra.system.bash.called",
                {"agent_id": "agent-custom-policy", "command": "pwd"},
            )
        )

        assert result.approved is False
        assert result.reason == "input rejected: no custom deny"
        assert bus.events[0].name == "human.bash"
    finally:
        human_approval_service.input_func = previous_input_func


@pytest.mark.asyncio
async def test_agent_base_run_one_does_not_inject_run_id():
    agent = AgentBase("agent-no-run-payload", "No Run Payload", object(), object())
    handle = _FakeToolHandle()
    agent.tool_factory = _FakeToolFactory(handle)
    agent.states["run_id"] = "must-not-leak"

    await agent._run_one(ToolCall(tool_name="bash", arguments={"command": "pwd"}))

    assert handle.payload == {"command": "pwd", "agent_id": "agent-no-run-payload"}


@pytest.mark.asyncio
async def test_streaming_observable_llm_publishes_executor_delta_and_structured_events():
    container = get_container()
    run_id = f"llm-stream-executor-run-{uuid.uuid4()}"
    previous_run_context = get_run_context_provider()
    chunks = [
        '{"think":"先检查目录",',
        '"tool_calls":[{"tool_name":"bash","arguments":{"command":"pwd"},"reasoning":"需要确认当前路径"}],',
        '"is_finished":false}',
    ]
    try:
        register_run_context_provider(_StaticRunContextProvider(run_id))
        llm = StreamingObservableLLMClient(
            _StreamingFakeLLM(chunks),
            container.events,
            agent_id="executor-stream-agent",
            agent_name="Executor Stream Agent",
            agent_type="executor",
        )

        result = await llm.chat([{"role": "system", "content": "executor"}, {"role": "user", "content": "go"}])
        events = container.events.list_events(run_id)
        names = [event["name"] for event in events]

        assert result == "".join(chunks)
        assert names.count("llm.delta") == 3
        assert "llm.started" in names
        assert "llm.completed" in names
        assert "agent.think" in names
        assert "agent.tool.reasoning" in names
        reasoning = [event for event in events if event["name"] == "agent.tool.reasoning"][-1]
        assert reasoning["payload"]["tool_name"] == "bash"
        assert reasoning["payload"]["reasoning"] == "需要确认当前路径"
    finally:
        register_run_context_provider(previous_run_context)


@pytest.mark.asyncio
async def test_streaming_observable_llm_publishes_planner_events():
    container = get_container()
    run_id = f"llm-stream-planner-run-{uuid.uuid4()}"
    previous_run_context = get_run_context_provider()
    chunks = [
        '{"steps":[{"step_id":"1","title":"分析需求","instruction":"阅读输入",',
        '"executor_id":"default_executor","depends_on":[]}]}',
    ]
    try:
        register_run_context_provider(_StaticRunContextProvider(run_id))
        llm = StreamingObservableLLMClient(
            _StreamingFakeLLM(chunks),
            container.events,
            agent_id="planner-stream-agent",
            agent_name="Planner Stream Agent",
            agent_type="planner",
        )

        result = await llm.chat(
            [
                {"role": "system", "content": "请生成结构化计划，输出 steps"},
                {"role": "user", "content": "go"},
            ]
        )
        events = container.events.list_events(run_id)
        plan_event = [event for event in events if event["name"] == "planner.plan.generated"][-1]

        assert result == "".join(chunks)
        assert plan_event["payload"]["planner_id"] == "planner-stream-agent"
        assert plan_event["payload"]["steps"][0]["title"] == "分析需求"
    finally:
        register_run_context_provider(previous_run_context)
