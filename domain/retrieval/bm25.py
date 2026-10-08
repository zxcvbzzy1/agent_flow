"""A dependency-free BM25 index over plain-text documents.

Callers map their own records (memory blocks, skills, ...) to strings and back;
scores are raw BM25 values (unbounded, 0 means no term overlap).
"""
from __future__ import annotations

import math
from collections import Counter
from typing import Callable, Sequence

from domain.retrieval.tokenize import tokenize


class BM25Index:
    def __init__(self, documents: Sequence[str], *, k1: float = 1.5, b: float = 0.75,
                 tokenizer: Callable[[str], list[str]] = tokenize):
        self.k1, self.b, self.tokenizer = k1, b, tokenizer
        self.docs = [Counter(tokenizer(document)) for document in documents]
        self.lengths = [sum(doc.values()) for doc in self.docs]
        self.average = sum(self.lengths) / len(self.docs) if self.docs else 0.0
        self.frequencies = Counter(term for doc in self.docs for term in doc)

    def __len__(self) -> int:
        return len(self.docs)

    def scores(self, query: str) -> list[float]:
        """One score per document, in document order."""
        terms = set(self.tokenizer(query))
        if not terms or not self.average:
            return [0.0] * len(self.docs)
        total = len(self.docs)
        results = []
        for doc, length in zip(self.docs, self.lengths):
            score = 0.0
            for term in terms & doc.keys():
                frequency, df = doc[term], self.frequencies[term]
                idf = math.log1p((total - df + 0.5) / (df + 0.5))
                score += idf * frequency * (self.k1 + 1) / (
                    frequency + self.k1 * (1 - self.b + self.b * length / self.average))
            results.append(score)
        return results
