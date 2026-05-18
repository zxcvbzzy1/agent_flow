from __future__ import annotations

from pydantic import BaseModel, Field


class RunCreateRequest(BaseModel):
    prompt: str
    planner_agent_id: str = "default_planner"
    executor_agent_ids: list[str] = Field(default_factory=lambda: ["default_executor"])
    context_id: str = "default_step"
    max_replan_rounds: int = 3
    conversation_id: str | None = None
    message_id: str | None = None
    auto_start: bool = True
