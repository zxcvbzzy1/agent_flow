"""SkillRegistry：技能记忆的存储与索引。

存储与“检索算法”解耦：Registry 负责保存 Skill 并用注入的 Embedder 预计算其向量；
具体怎么算相似度、怎么排序在 SkillRetriever 里。Registry 本身不关心来源
（文件 / DB / RAG），由 loader 或应用层把 Skill 灌进来。
"""

from __future__ import annotations

from typing import Iterable

from domain.skill.skill import Skill
from domain.skill.vectorizer import BagOfWordsEmbedder, Embedder


class SkillRegistry:
    def __init__(self, embedder: Embedder | None = None) -> None:
        self._embedder: Embedder = embedder or BagOfWordsEmbedder()
        self._skills: dict[str, Skill] = {}
        self._vectors: dict[str, dict[str, float]] = {}

    @property
    def embedder(self) -> Embedder:
        return self._embedder

    def add(self, skill: Skill) -> None:
        self._skills[skill.id] = skill
        self._vectors[skill.id] = self._embedder.embed(skill.index_text())

    def add_many(self, skills: Iterable[Skill]) -> None:
        for skill in skills:
            self.add(skill)

    def get(self, skill_id: str) -> Skill | None:
        return self._skills.get(skill_id)

    def all(self) -> list[Skill]:
        return list(self._skills.values())

    def vector(self, skill_id: str) -> dict[str, float]:
        return self._vectors.get(skill_id, {})

    def items_with_vectors(self) -> list[tuple[Skill, dict[str, float]]]:
        return [(skill, self._vectors.get(skill.id, {})) for skill in self._skills.values()]

    def remove(self, skill_id: str) -> None:
        self._skills.pop(skill_id, None)
        self._vectors.pop(skill_id, None)

    def clear(self) -> None:
        self._skills.clear()
        self._vectors.clear()

    def __len__(self) -> int:
        return len(self._skills)
