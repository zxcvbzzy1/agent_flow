from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class AgentCreateRequest(BaseModel):
    name: str
    agent_type: str = "executor"
    context_id: str = "default_executor"
    role_prompt: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

