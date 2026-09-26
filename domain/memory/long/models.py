"""Long-term memory documents. IDs and line references survive content revisions."""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class MemoryScope:
    user_id: str
    room_id: str = "standalone"
    conversation_id: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MemorySource:
    source_id: str
    user_id: str
    room_id: str
    conversation_id: str
    file_path: str
    content_hash: str
    source_kind: str = "run"
    run_id: str = ""
    message_id: str = ""
    run_status: str = ""
    index_status: str = "pending"
    deleted_at: float | None = None
    # Persist section boundaries so retries never need to parse user Markdown.
    sections: list[dict] = field(default_factory=list)
    block_defaults: dict = field(default_factory=dict)
    error: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    schema_version: int = 1

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MemoryBlock:
    block_id: str
    memory_id: str
    source_id: str
    user_id: str
    room_id: str
    conversation_id: str
    file_path: str
    start_line: int
    end_line: int
    content: str
    content_hash: str
    token_count: int
    token_count_method: str
    version: int = 1
    schema_version: int = 1
    run_id: str = ""
    message_id: str = ""
    run_status: str = ""
    event: dict = field(default_factory=dict)
    section_kind: str = "qa"
    memory_type: str = "unclassified"
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    status: str = "active"
    expires_at: float | None = None
    deleted_at: float | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    usage_count: int = 0
    last_used_at: float | None = None
    derived_from_block_ids: list[str] = field(default_factory=list)
    superseded_by_block_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)
