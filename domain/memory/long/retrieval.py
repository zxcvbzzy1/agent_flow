"""A replaceable, dependency-free BM25 retriever for the current user's corpus."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from domain.retrieval import BM25Index, tokenize  # noqa: F401  (tokenize re-exported for existing imports)


@dataclass(frozen=True)
class RecallOptions:
    """Parameters used by one agent run's retrieval, supplied by its host."""

    limit: int = 6
    token_budget: int = 4000
    k1: float = 1.5
    b: float = 0.75

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RecallResult:
    config: dict
    corpus_count: int = 0
    matched_count: int = 0
    selected: list[dict] = field(default_factory=list)
    diagnostics: list[dict] = field(default_factory=list)
    diagnostics_truncated: bool = False
    context: str = ""
    token_count: int = 0
    token_count_method: str = ""
    empty_reason: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def effective_status(block: dict, source: dict | None, now: float, *,
                     sources: dict | None = None, completed_batches: set | None = None) -> str:
    """State shared by runtime retrieval and the IM management view."""
    evidence = [sources.get(sid) for sid in block.get("evidence_source_ids", [])] if sources is not None else []
    if block["status"] == "deleted" or (source and source.get("deleted_at") is not None) or any(
            s and s.get("deleted_at") is not None for s in evidence):
        return "deleted"
    if block["status"] != "active":
        return block["status"]
    if block.get("expires_at") is not None and block["expires_at"] <= now:
        return "expired"
    if not source or source.get("index_status") != "ready":
        return "unavailable"
    if any(not s or s.get("index_status") != "ready" for s in evidence):
        return "unavailable"
    if block.get("batch_id") and (completed_batches is None or block["batch_id"] not in completed_batches):
        return "unavailable"
    return "active"


class BM25Retriever:
    """Memory-block adapter over the shared BM25Index."""

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b

    def rank(self, query: str, blocks: list[dict]) -> list[dict]:
        if not blocks:
            return []
        scores = BM25Index([block["content"] for block in blocks], k1=self.k1, b=self.b).scores(query)
        results = [{**block, "score": score} for block, score in zip(blocks, scores) if score > 0]
        return sorted(results, key=lambda block: (-block["score"], -block["updated_at"], block["block_id"]))
