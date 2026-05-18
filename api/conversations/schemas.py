from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ConversationCreateRequest(BaseModel):
    title: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class MessageCreateRequest(BaseModel):
    role: str
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    run_id: str | None = None


class QueueCreateRequest(BaseModel):
    message_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConversationRunCreateRequest(BaseModel):
    planner_agent_id: str = "default_planner"
    executor_agent_ids: list[str] = Field(default_factory=lambda: ["default_executor"])
    context_id: str = "default_step"
    max_replan_rounds: int = 3

