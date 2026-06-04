"""SkillRetriever：把“查询 -> 相关技能”这一步做成可替换模块。

现在的实现 VectorSkillRetriever 用 Registry 里预计算的词频向量 + 余弦相似度排序。
后续接 RAG 时，实现一个同样满足 BaseSkillRetriever.retrieve(query, k, threshold)
的检索器（内部走向量库/重排），系统检索召回与 recall_skill 工具都无需改动。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from domain.skill.registry import SkillRegistry
from domain.skill.skill import Skill
from domain.skill.vectorizer import cosine_similarity


@dataclass
class SkillHit:
    """一次召回结果：命中的技能 + 相关度分数。"""

    skill: Skill
    score: float


class BaseSkillRetriever(ABC):
    @abstractmethod
    def retrieve(self, query: str, k: int = 5, threshold: float = 0.0) -> list[SkillHit]:
        ...


class VectorSkillRetriever(BaseSkillRetriever):
    def __init__(self, registry: SkillRegistry) -> None:
        self._registry = registry

    @property
    def registry(self) -> SkillRegistry:
        return self._registry

    def retrieve(self, query: str, k: int = 5, threshold: float = 0.0) -> list[SkillHit]:
        query = (query or "").strip()
        if not query or len(self._registry) == 0:
            return []
        query_vec = self._registry.embedder.embed(query)
        hits: list[SkillHit] = []
        for skill, vec in self._registry.items_with_vectors():
            score = cosine_similarity(query_vec, vec)
            if score > threshold:
                hits.append(SkillHit(skill=skill, score=score))
        hits.sort(key=lambda hit: hit.score, reverse=True)
        if k and k > 0:
            hits = hits[:k]
        return hits
