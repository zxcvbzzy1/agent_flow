from __future__ import annotations

from typing import Any
from typing import Literal

from pydantic import BaseModel, Field


class ConversationCreateRequest(BaseModel):
    title: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class MessageCreateRequest(BaseModel):
    role: str
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    run_id: str | None = None


class ConversationRunCreateRequest(BaseModel):
    message_id: str
    mode: Literal["react", "plan"] = "plan"
    executor_agent_id: str | None = None
    planner_agent_id: str = "default_planner"
    executor_agent_ids: list[str] = Field(default_factory=lambda: ["default_executor"])
    context_id: str = "default_step"
    max_replan_rounds: int = 3
    auto_start: bool = True
