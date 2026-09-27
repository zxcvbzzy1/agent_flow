from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class SourceFiles(Protocol):
    def write(self, path: str, content: str) -> None: ...
    def read(self, path: str) -> str: ...


class MemoryRepository(Protocol):
    def completed_batches(self, user_id: str) -> set[str]: ...
    def get_source(self, source_id: str) -> dict | None: ...
    def sources(self, query: dict) -> list[dict]: ...
    def put_source(self, document: dict) -> dict: ...
    def update_source(self, source_id: str, changes: dict) -> None: ...
    def blocks(self, query: dict) -> list[dict]: ...
    def put_block(self, document: dict) -> dict: ...
    def update_block(self, block_id: str, changes: dict) -> None: ...
    def mark_used(self, block_ids: list[str], run_id: str) -> None: ...


class TokenCounter(Protocol):
    name: str
    def count(self, text: str) -> int: ...


@dataclass
class RoutingDecision:
    include: bool = True
    memory_type: str = "unclassified"
    tags: list[str] = field(default_factory=list)
    expires_at: float | None = None


class MemoryRouter(Protocol):
    """Synchronous rule-mode routing; async batch classifiers use processing.py."""
    def route(self, source: dict, candidate: dict) -> RoutingDecision: ...


class AcceptAllRouter:
    def route(self, source: dict, candidate: dict) -> RoutingDecision:
        return RoutingDecision()


class MemoryRetriever(Protocol):
    def rank(self, query: str, blocks: list[dict]) -> list[dict]: ...
