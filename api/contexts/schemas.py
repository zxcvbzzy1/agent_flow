from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class ContextCreateRequest(BaseModel):
    name: str
    kind: str = "executor"
    provider_config: list[dict[str, Any]] | None = None
