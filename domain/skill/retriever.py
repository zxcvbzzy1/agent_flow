"""SkillRetriever：把“查询 -> 相关技能”这一步做成可替换模块。

现在的实现 BM25SkillRetriever 用共享的 BM25（domain.retrieval）对 Skill.index_text()
打分排序，分数为原始 BM25 值（无上界，0 表示无词项重合）。后续接 RAG 时，实现一个
同样满足 BaseSkillRetriever.retrieve(query, k, threshold) 的检索器（内部走向量库/重排），
系统检索召回与 recall_skill 工具都无需改动。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from domain.retrieval import BM25Index
from domain.skill.registry import SkillRegistry
from domain.skill.skill import Skill


@dataclass
class SkillHit:
    """一次召回结果：命中的技能 + 相关度分数。"""

    skill: Skill
    score: float


class BaseSkillRetriever(ABC):
    @abstractmethod
    def retrieve(self, query: str, k: int = 5, threshold: float = 0.0) -> list[SkillHit]:
        ...


class BM25SkillRetriever(BaseSkillRetriever):
    def __init__(self, registry: SkillRegistry, *, k1: float = 1.5, b: float = 0.75) -> None:
        self._registry = registry
        self.k1, self.b = k1, b
        # (registry.version, skills, index)：技能库变更后在下一次检索时惰性重建
        self._cache: tuple[int, list[Skill], BM25Index] | None = None

    @property
    def registry(self) -> SkillRegistry:
        return self._registry

    def _index(self) -> tuple[list[Skill], BM25Index]:
        version = self._registry.version
        if self._cache is None or self._cache[0] != version:
            skills = self._registry.all()
            index = BM25Index([skill.index_text() for skill in skills], k1=self.k1, b=self.b)
            self._cache = (version, skills, index)
        return self._cache[1], self._cache[2]

    def retrieve(self, query: str, k: int = 5, threshold: float = 0.0) -> list[SkillHit]:
        query = (query or "").strip()
        if not query or len(self._registry) == 0:
            return []
        skills, index = self._index()
        hits = [SkillHit(skill=skill, score=score)
                for skill, score in zip(skills, index.scores(query)) if score > threshold]
        hits.sort(key=lambda hit: (-hit.score, hit.skill.id))
        if k and k > 0:
            hits = hits[:k]
        return hits
