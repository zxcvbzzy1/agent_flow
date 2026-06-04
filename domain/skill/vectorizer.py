"""向量化与相似度。

这是“可替换为 RAG 的接缝”：现在用最简单的词频(bag-of-words)稀疏向量 + 余弦相似度，
零外部依赖、对中英文混排都能工作。后续要换成真正的 embedding/RAG，只需实现同样的
`Embedder` 接口（embed(text) -> 稀疏或稠密向量）并提供配套的相似度函数即可，
上层 SkillRegistry / SkillRetriever 无需改动。
"""

from __future__ import annotations

import math
import re
from abc import ABC, abstractmethod
from collections import Counter

# 英文/数字按词切分；中文按字（unigram）+ 相邻字（bigram）切分。
_WORD_RE = re.compile(r"[a-zA-Z0-9_]+")


def _is_cjk(ch: str) -> bool:
    return "一" <= ch <= "鿿"


def tokenize(text: str) -> list[str]:
    """把文本切成 token 列表（小写英文词 + 中文单字 + 中文相邻二元）。"""
    text = (text or "").lower()
    tokens: list[str] = _WORD_RE.findall(text)
    cjk = [ch for ch in text if _is_cjk(ch)]
    tokens.extend(cjk)
    tokens.extend(cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1))
    return tokens


class Embedder(ABC):
    """文本向量化接口。实现 embed() 即可替换底层算法（词频 / TF-IDF / RAG embedding）。"""

    @abstractmethod
    def embed(self, text: str) -> dict[str, float]:
        ...


class BagOfWordsEmbedder(Embedder):
    """词频稀疏向量。term -> count。余弦相似度会自动归一化，这里不必预归一。"""

    def embed(self, text: str) -> dict[str, float]:
        counts = Counter(tokenize(text))
        return {term: float(c) for term, c in counts.items()}


def cosine_similarity(a: dict[str, float], b: dict[str, float]) -> float:
    """两个稀疏向量的余弦相似度，范围 [0, 1]（词频非负）。"""
    if not a or not b:
        return 0.0
    # 遍历较小的那个，省一点点
    if len(a) > len(b):
        a, b = b, a
    dot = 0.0
    for term, va in a.items():
        vb = b.get(term)
        if vb:
            dot += va * vb
    if dot == 0.0:
        return 0.0
    norm_a = math.sqrt(sum(v * v for v in a.values()))
    norm_b = math.sqrt(sum(v * v for v in b.values()))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)
