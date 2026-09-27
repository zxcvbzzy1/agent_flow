"""A replaceable, dependency-free BM25 retriever for the current user's corpus."""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import asdict, dataclass, field


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


def effective_status(block: dict, source: dict | None, now: float) -> str:
    """State shared by runtime retrieval and the IM management view."""
    if block["status"] == "deleted" or (source and source.get("deleted_at") is not None):
        return "deleted"
    if block["status"] != "active":
        return block["status"]
    if block.get("expires_at") is not None and block["expires_at"] <= now:
        return "expired"
    if not source or source.get("index_status") != "ready":
        return "unavailable"
    return "active"


def tokenize(text: str) -> list[str]:
    terms = re.findall(r"[a-z0-9_]+", text.lower())
    for segment in re.findall(r"[\u3400-\u9fff]+", text):
        terms.extend(segment)
        terms.extend(segment[i:i + 2] for i in range(len(segment) - 1))
    return terms


class BM25Retriever:
    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b

    def rank(self, query: str, blocks: list[dict]) -> list[dict]:
        terms = set(tokenize(query))
        if not terms or not blocks:
            return []
        docs = [Counter(tokenize(block["content"])) for block in blocks]
        lengths = [sum(doc.values()) for doc in docs]
        average = sum(lengths) / len(docs)
        if not average:
            return []
        frequencies = Counter(term for doc in docs for term in doc)
        results = []
        for block, doc, length in zip(blocks, docs, lengths):
            score = 0.0
            for term in terms & doc.keys():
                frequency = doc[term]
                idf = math.log1p((len(docs) - frequencies[term] + 0.5) / (frequencies[term] + 0.5))
                score += idf * frequency * (self.k1 + 1) / (
                    frequency + self.k1 * (1 - self.b + self.b * length / average))
            if score > 0:
                results.append({**block, "score": score})
        return sorted(results, key=lambda block: (-block["score"], -block["updated_at"], block["block_id"]))
