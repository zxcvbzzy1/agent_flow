"""Dependency-free tokenizer shared by lexical retrievers (skills, long-term memory)."""
from __future__ import annotations

import re


def tokenize(text: str) -> list[str]:
    """Lowercase ASCII words plus CJK unigrams and in-segment bigrams."""
    text = text or ""
    terms = re.findall(r"[a-z0-9_]+", text.lower())
    for segment in re.findall(r"[㐀-鿿]+", text):
        terms.extend(segment)
        terms.extend(segment[i:i + 2] for i in range(len(segment) - 1))
    return terms
