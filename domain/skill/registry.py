"""SkillRegistry：技能记忆的存储。

存储与“检索算法”解耦：Registry 只保存 Skill，并在每次变更时递增 version；
检索器（BM25 / 后续 RAG）按 version 判断是否需要重建自己的索引。Registry 本身
不关心来源（文件 / DB / RAG），由 loader 或应用层把 Skill 灌进来。
"""

from __future__ import annotations

from typing import Iterable

from domain.skill.skill import Skill


class SkillRegistry:
    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}
        self._version = 0

    @property
    def version(self) -> int:
        """每次增删改递增，供检索器做索引失效判断。"""
        return self._version

    def add(self, skill: Skill) -> None:
        self._skills[skill.id] = skill
        self._version += 1

    def add_many(self, skills: Iterable[Skill]) -> None:
        for skill in skills:
            self.add(skill)

    def get(self, skill_id: str) -> Skill | None:
        return self._skills.get(skill_id)

    def all(self) -> list[Skill]:
        return list(self._skills.values())

    def remove(self, skill_id: str) -> None:
        if self._skills.pop(skill_id, None) is not None:
            self._version += 1

    def clear(self) -> None:
        self._skills.clear()
        self._version += 1

    def __len__(self) -> int:
        return len(self._skills)
