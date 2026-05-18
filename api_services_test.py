from __future__ import annotations

from fastapi.testclient import TestClient

from api.core.dependencies import get_container
from api.index import app


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

