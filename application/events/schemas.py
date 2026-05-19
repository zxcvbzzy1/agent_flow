from __future__ import annotations

import time
from typing import Any


def tool_event_payload(
    *,
    run_id: str,
    agent_id: str,
    tool_name: str,
    tool_field: str | None,
    internal_event_name: str,
    frontend_event_name: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "agent_id": agent_id,
        "tool_name": tool_name,
        "tool_field": tool_field,
        "event_name": internal_event_name,
        "frontend_event_name": frontend_event_name,
        "arguments": payload if frontend_event_name == "tool.called" else payload.get("arguments", {}),
        "respond": payload.get("respond"),
        "success": payload.get("success"),
        "created_at": time.time(),
    }


def step_failed_payload(
    *,
    run_id: str,
    executor_id: str,
    step: dict[str, Any],
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "executor_id": executor_id,
        "agent_id": executor_id,
        "step_id": step.get("step_id", ""),
        "step_title": step.get("title", ""),
        "status_reason": step.get("status_reason", ""),
        "result_observation": step.get("result_observation", ""),
        "created_at": time.time(),
    }

