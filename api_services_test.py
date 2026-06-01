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
from domain.state import Plan
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


def _executor_provider_config() -> list[dict]:
    return [
        {"provider_id": "user_prompt", "enabled": True, "params": {}},
        {
            "provider_id": "available_tools",
            "enabled": True,
            "params": {"available_fields": ["system"]},
        },
        {
            "provider_id": "history",
            "enabled": True,
            "params": {
                "memory_field": "agent_history",
                "strategy_config": {
                    "pipeline": [
                        {"type": "full_history"},
                        {"type": "recency", "keep_last": 2},
                    ]
                },
            },
        },
    ]


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
    assert any(item["name"] == "inline_artifact" for item in tools)

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

    catalog = client.get("/api/contexts/catalog")
    assert catalog.status_code == 200
    assert "providers" in catalog.json()["item"]

    contexts = client.get("/api/contexts")
    assert contexts.status_code == 200
    assert any(item["context_id"] == "default_executor" for item in contexts.json()["items"])

    missing_providers = client.post(
        "/api/contexts",
        json={"name": "Invalid Context", "kind": "executor"},
    )
    assert missing_providers.status_code == 400

    unknown_strategy = client.post(
        "/api/contexts",
        json={
            "name": "Invalid Strategy",
            "kind": "executor",
            "provider_config": [
                {
                    "provider_id": "history",
                    "enabled": True,
                    "params": {
                        "memory_field": "agent_history",
                        "strategy_config": {"pipeline": [{"type": "not_a_strategy"}]},
                    },
                }
            ],
        },
    )
    assert unknown_strategy.status_code == 400

    context = client.post(
        "/api/contexts",
        json={
            "name": "API Test Context",
            "kind": "executor",
            "provider_config": _executor_provider_config(),
        },
    ).json()["item"]
    assert client.get(f"/api/contexts/{context['context_id']}").status_code == 200
    assert context["provider_count"] == 3

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
    conversation_run = client.post(
        f"/api/conversations/{conversation['conversation_id']}/runs",
        json={
            "mode": "plan",
            "message_id": message["message_id"],
            "planner_agent_id": "default_planner",
            "executor_agent_ids": ["default_executor"],
            "context_id": "default_step",
            "auto_start": False,
        },
    ).json()["item"]
    updated_message = client.get(
        f"/api/conversations/{conversation['conversation_id']}/messages",
    ).json()["items"][-1]

    assert conversation_run["conversation_id"] == conversation["conversation_id"]
    assert conversation_run["message_id"] == message["message_id"]
    assert updated_message["run_id"] == conversation_run["run_id"]
    assert client.post(f"/api/conversations/{conversation['conversation_id']}/queue").status_code == 404
    assert client.get(f"/api/conversations/{conversation['conversation_id']}/queue").status_code == 404


def test_cancel_pending_run_marks_cancelled_and_publishes_workflow_failed():
    client = TestClient(app)
    container = get_container()

    run = client.post(
        "/api/runs",
        json={
            "prompt": "cancel pending run",
            "planner_agent_id": "default_planner",
            "executor_agent_ids": ["default_executor"],
            "context_id": "default_step",
            "auto_start": False,
        },
    ).json()["item"]

    response = client.post(f"/api/runs/{run['run_id']}/cancel")

    assert response.status_code == 200
    assert response.json()["item"]["status"] == "cancelled"
    stored = client.get(f"/api/runs/{run['run_id']}").json()["item"]
    assert stored["status"] == "cancelled"
    events = container.events.list_events(run["run_id"])
    assert events[-1]["name"] == "workflow.failed"
    assert events[-1]["payload"]["cancelled"] is True
    assert client.post(f"/api/runs/{run['run_id']}/cancel").status_code == 200
    assert client.post("/api/runs/not-found/cancel").status_code == 404


def test_runs_list_and_create_validation():
    client = TestClient(app)

    empty_executors = client.post(
        "/api/runs",
        json={
            "prompt": "missing executor",
            "planner_agent_id": "default_planner",
            "executor_agent_ids": [],
            "context_id": "default_step",
            "auto_start": False,
        },
    )
    assert empty_executors.status_code == 400

    missing_context = client.post(
        "/api/runs",
        json={
            "prompt": "missing context",
            "planner_agent_id": "default_planner",
            "executor_agent_ids": ["default_executor"],
            "context_id": "missing_step_context",
            "auto_start": False,
        },
    )
    assert missing_context.status_code == 404

    run = client.post(
        "/api/runs",
        json={
            "prompt": "list visible run",
            "planner_agent_id": "default_planner",
            "executor_agent_ids": ["default_executor"],
            "context_id": "default_step",
            "auto_start": False,
        },
    ).json()["item"]
    listed = client.get("/api/runs")

    assert listed.status_code == 200
    assert any(item["run_id"] == run["run_id"] for item in listed.json()["items"])


@pytest.mark.asyncio
async def test_react_run_uses_start_with_history_and_writes_assistant_message():
    container = get_container()
    conversation = container.conversations.create_conversation("React Chat")
    container.conversations.add_message(
        conversation_id=conversation["conversation_id"],
        role="user",
        content="previous user",
    )
    container.conversations.add_message(
        conversation_id=conversation["conversation_id"],
        role="assistant",
        content="previous assistant",
    )
    user_message = container.conversations.add_message(
        conversation_id=conversation["conversation_id"],
        role="user",
        content="react hello",
    )
    executor = container.agents.get_agent("default_executor")
    original_start_with_history = executor.start_with_history
    called_prompts = []

    async def fake_start_with_history(prompt: str) -> None:
        called_prompts.append(prompt)
        executor.states["prompt"] = prompt
        executor.states["final"] = "react final"
        executor.states["finish_reason"] = "react done"
        executor.states["is_finished"] = True

    executor.start_with_history = fake_start_with_history
    try:
        run = container.runs.create_run(
            prompt=user_message["content"],
            mode="react",
            executor_agent_id="default_executor",
            conversation_id=conversation["conversation_id"],
            message_id=user_message["message_id"],
            auto_start=True,
        )
        await asyncio.sleep(0.05)
    finally:
        executor.start_with_history = original_start_with_history

    stored = container.runs.get_run(run["run_id"])
    messages = container.conversations.list_messages(conversation["conversation_id"])
    memory = executor.context_engine.get_memory()
    history = "\n".join(
        memory.get("agent_history", "dialogue", index + 1)
        for index in range(memory.count("agent_history", "dialogue"))
    )

    assert called_prompts == ["react hello"]
    assert "previous user" in history
    assert "previous assistant" in history
    assert "react hello" not in history
    assert stored["status"] == "finished"
    assert stored["final"] == "react final"
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["content"] == "react final"
    assert messages[-1]["run_id"] == run["run_id"]


@pytest.mark.asyncio
async def test_conversation_history_is_isolated_between_react_runs():
    container = get_container()
    first = container.conversations.create_conversation("First Chat")
    container.conversations.add_message(first["conversation_id"], "user", "first previous")
    container.conversations.add_message(first["conversation_id"], "assistant", "first assistant")

    second = container.conversations.create_conversation("Second Chat")
    container.conversations.add_message(second["conversation_id"], "user", "second previous")
    current = container.conversations.add_message(second["conversation_id"], "user", "second current")

    executor = container.agents.get_agent("default_executor")
    original_start_with_history = executor.start_with_history

    async def fake_start_with_history(prompt: str) -> None:
        executor.states["final"] = "isolated final"
        executor.states["finish_reason"] = "done"
        executor.states["is_finished"] = True

    executor.start_with_history = fake_start_with_history
    try:
        container.runs.create_run(
            prompt=current["content"],
            mode="react",
            executor_agent_id="default_executor",
            conversation_id=second["conversation_id"],
            message_id=current["message_id"],
            auto_start=True,
        )
        await asyncio.sleep(0.05)
    finally:
        executor.start_with_history = original_start_with_history

    memory = executor.context_engine.get_memory()
    history = "\n".join(
        memory.get("agent_history", "dialogue", index + 1)
        for index in range(memory.count("agent_history", "dialogue"))
    )

    assert "second previous" in history
    assert "first previous" not in history
    assert "first assistant" not in history
    assert "second current" not in history


@pytest.mark.asyncio
async def test_plan_run_loads_conversation_history_into_planner_memory():
    container = get_container()
    conversation = container.conversations.create_conversation("Plan Chat")
    container.conversations.add_message(conversation["conversation_id"], "user", "plan previous")
    container.conversations.add_message(conversation["conversation_id"], "assistant", "plan assistant")
    current = container.conversations.add_message(conversation["conversation_id"], "user", "plan current")

    planner = container.agents.get_agent("default_planner")
    original_generate_plan = planner.generate_plan
    original_summarize_result = planner.summarize_result
    observed_history = []

    async def fake_generate_plan(state, executor_ids):
        memory = planner.context_engine.get_memory()
        observed_history.append("\n".join(
            memory.get("agent_history", "dialogue", index + 1)
            for index in range(memory.count("agent_history", "dialogue"))
        ))
        return Plan()

    async def fake_summarize_result(state):
        return "plan final"

    planner.generate_plan = fake_generate_plan
    planner.summarize_result = fake_summarize_result
    try:
        container.runs.create_run(
            prompt=current["content"],
            mode="plan",
            planner_agent_id="default_planner",
            executor_agent_ids=["default_executor"],
            context_id="default_step",
            conversation_id=conversation["conversation_id"],
            message_id=current["message_id"],
            auto_start=True,
        )
        await asyncio.sleep(0.05)
    finally:
        planner.generate_plan = original_generate_plan
        planner.summarize_result = original_summarize_result

    assert observed_history
    assert "plan previous" in observed_history[0]
    assert "plan assistant" in observed_history[0]
    assert "plan current" not in observed_history[0]


def test_cancel_run_does_not_write_assistant_message():
    client = TestClient(app)

    conversation = client.post(
        "/api/conversations",
        json={"title": "Cancel Run"},
    ).json()["item"]
    message = client.post(
        f"/api/conversations/{conversation['conversation_id']}/messages",
        json={"role": "user", "content": "cancel direct run"},
    ).json()["item"]
    run = client.post(
        f"/api/conversations/{conversation['conversation_id']}/runs",
        json={
            "mode": "plan",
            "message_id": message["message_id"],
            "planner_agent_id": "default_planner",
            "executor_agent_ids": ["default_executor"],
            "context_id": "default_step",
            "auto_start": False,
        },
    ).json()["item"]

    response = client.post(f"/api/runs/{run['run_id']}/cancel")
    messages = client.get(f"/api/conversations/{conversation['conversation_id']}/messages").json()["items"]

    assert response.status_code == 200
    assert response.json()["item"]["status"] == "cancelled"
    assert all(item["role"] != "assistant" for item in messages)


def test_delete_conversation_cascades_messages_runs_and_events():
    client = TestClient(app)
    container = get_container()

    conversation = client.post(
        "/api/conversations",
        json={"title": "Delete Cascade"},
    ).json()["item"]
    message_item = client.post(
        f"/api/conversations/{conversation['conversation_id']}/messages",
        json={"role": "user", "content": "delete me"},
    ).json()["item"]
    run = client.post(
        f"/api/conversations/{conversation['conversation_id']}/runs",
        json={
            "mode": "plan",
            "message_id": message_item["message_id"],
            "planner_agent_id": "default_planner",
            "executor_agent_ids": ["default_executor"],
            "context_id": "default_step",
            "max_replan_rounds": 1,
        },
    ).json()["item"]
    container.events.publish(run["run_id"], "workflow.started", {"ok": True})

    response = client.delete(f"/api/conversations/{conversation['conversation_id']}")

    assert response.status_code == 200
    assert response.json()["item"]["deleted"] is True
    assert container.store.find_one("conversations", {"conversation_id": conversation["conversation_id"]}) is None
    assert container.store.find_many("messages", {"conversation_id": conversation["conversation_id"]}) == []
    assert container.store.find_one("runs", {"run_id": run["run_id"]}) is None
    assert container.events.list_events(run["run_id"]) == []


def test_delete_uploaded_tool_and_protect_builtin_tool():
    client = TestClient(app)
    tool_name = f"delete_tool_{uuid.uuid4().hex[:8]}"
    upload = client.post(
        "/api/tools/upload",
        json={
            "name": tool_name,
            "description": "delete test",
            "field": "system",
            "input_schema": {"type": "object", "properties": {}},
            "metadata": {},
            "source_code": "",
        },
    )

    assert upload.status_code == 200
    assert client.delete(f"/api/tools/{tool_name}").status_code == 200
    tools = client.get("/api/tools").json()["items"]
    assert all(item["name"] != tool_name for item in tools)
    assert client.delete("/api/tools/bash").status_code == 400


def test_delete_agent_cleans_runtime_and_related_runs():
    client = TestClient(app)
    container = get_container()
    agent = client.post(
        "/api/agents",
        json={
            "name": "Delete Agent",
            "agent_type": "executor",
            "context_id": "default_executor",
        },
    ).json()["item"]
    run = client.post(
        "/api/runs",
        json={
            "prompt": "delete agent run",
            "planner_agent_id": "default_planner",
            "executor_agent_ids": [agent["agent_id"]],
            "context_id": "default_step",
            "auto_start": False,
        },
    ).json()["item"]
    container.events.publish(run["run_id"], "workflow.started", {"ok": True})

    response = client.delete(f"/api/agents/{agent['agent_id']}")

    assert response.status_code == 200
    assert response.json()["item"]["deleted"] is True
    assert container.store.find_one("agents", {"agent_id": agent["agent_id"]}) is None
    assert agent["agent_id"] not in container.agents._agents
    assert container.store.find_one("runs", {"run_id": run["run_id"]}) is None
    assert container.events.list_events(run["run_id"]) == []
    assert client.delete("/api/agents/default_executor").status_code == 400


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


def test_frontend_bridge_mirrors_artifacts_event_to_run_stream():
    container = get_container()
    run_id = "artifact-bridge-test-run"

    container.frontend_bridge.register_agent_run("artifact_agent", run_id)
    container.frontend_bridge.mirror_tool_event(
        Event(
            name="artifacts.document",
            payload={
                "agent_id": "artifact_agent",
                "artifact_type": "document",
                "artifact": {
                    "type": "document",
                    "title": "Preview",
                    "content": "# hello",
                    "editable": True,
                },
                "created_at": 123.0,
            },
        )
    )

    events = container.events.list_events(run_id)

    assert events[-1]["name"] == "artifacts.document"
    assert events[-1]["payload"]["frontend_event_name"] == "artifacts.document"
    assert events[-1]["payload"]["artifact_type"] == "document"
    assert events[-1]["payload"]["artifact"]["title"] == "Preview"


def test_inline_artifact_tool_builds_defaults_and_validates():
    from infra.tool.builtin.artifacts import InlineArtifactTool

    payload = InlineArtifactTool().build_event_payload(
        {
            "agent_id": "artifact_agent",
            "artifact_type": "document",
            "document": {
                "title": "Doc",
                "content": "# doc",
                "format": "md",
            },
        }
    )

    assert payload["event_name"] == "artifacts.document"
    assert payload["artifact_type"] == "document"
    assert payload["artifact"]["type"] == "document"
    assert payload["artifact"]["editable"] is True
    assert payload["artifact"]["mime_type"] == "text/markdown"
    assert payload["artifact"]["language"] == "markdown"

    image_payload = InlineArtifactTool().build_event_payload(
        {
            "artifact_type": "image",
            "image": {"url": "https://example.test/image.png"},
        }
    )
    assert image_payload["artifact"]["editable"] is False
    assert image_payload["artifact"]["url"] == "https://example.test/image.png"

    with pytest.raises(ValueError, match="artifact_type"):
        InlineArtifactTool().build_event_payload(
            {"artifact_type": "unknown"}
        )

    with pytest.raises(ValueError, match="document"):
        InlineArtifactTool().build_event_payload(
            {"artifact_type": "document"}
        )


@pytest.mark.asyncio
async def test_inline_artifact_tool_called_event_publishes_artifact_sse():
    from infra.config import bus, factory

    container = get_container()
    run_id = "artifact-tool-test-run"
    agent_id = "artifact_tool_agent"
    container.frontend_bridge.register_agent_run(agent_id, run_id)

    await bus.publish(
        factory.tool("inline_artifact").called(
            {
                "agent_id": agent_id,
                "artifact_type": "document",
                "document": {
                    "title": "Inline Doc",
                    "content": "# inline",
                    "format": "md",
                },
            }
        )
    )

    events = container.events.list_events(run_id)
    event_names = [event["name"] for event in events]

    assert "tool.called" in event_names
    assert "artifacts.document" in event_names
    assert "tool.succeeded" in event_names
    artifact_event = next(event for event in events if event["name"] == "artifacts.document")
    assert artifact_event["payload"]["artifact"]["title"] == "Inline Doc"
    assert artifact_event["payload"]["artifact"]["editable"] is True


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
        assert names.count("llm.delta") == 0
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
