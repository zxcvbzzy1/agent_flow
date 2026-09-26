"""A replaceable, dependency-free BM25 retriever for the current user's corpus."""
from __future__ import annotations

import math
import re
from collections import Counter


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
