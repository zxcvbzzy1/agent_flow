from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ContextCreateRequest(BaseModel):
    name: str
    kind: str = "executor"
    provider_config: list[dict[str, Any]] = Field(default_factory=list)
    strategy_config: dict[str, Any] = Field(default_factory=lambda: {"type": "full_history"})
    available_fields: list[str] = Field(default_factory=lambda: ["system", "search", "memory", "write_agent", "human"])

