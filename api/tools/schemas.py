from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ToolUploadRequest(BaseModel):
    name: str
    description: str = ""
    field: str | None = None
    input_schema: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    source_code: str = ""

