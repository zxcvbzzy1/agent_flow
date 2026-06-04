"""Skill 数据模型。

Skill 不是固定 Prompt，而是一段可被检索召回、按需注入上下文的“记忆”
（某类任务的方法/步骤/要点）。它由 SkillRegistry 持有、SkillRetriever 检索，
最终经 SkillProvider 注入到 Agent 的上下文里。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Skill:
    """一条可召回的技能记忆。

    - id:          唯一标识（默认取文件名）
    - name:        技能名
    - description: 一句话说明（既给人看，也参与检索匹配）
    - content:     技能正文（真正可执行的方法/步骤/要点）
    - tags:        标签，辅助检索
    - source:      来源（文件路径等），便于追溯
    - metadata:    预留扩展位
    """

    id: str
    name: str
    description: str = ""
    content: str = ""
    tags: list[str] = field(default_factory=list)
    source: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def index_text(self) -> str:
        """用于向量化/匹配的文本。

        name/description/tags 是高信号字段，做加权（重复拼接）让标题与摘要
        在简单词频匹配里权重更高，content 作为补充召回信号。
        """
        name = (self.name or "").strip()
        desc = (self.description or "").strip()
        tags = " ".join(self.tags or [])
        parts = [name] * 3 + [desc] * 2 + [tags] * 2 + [self.content or ""]
        return " ".join(p for p in parts if p)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "content": self.content,
            "tags": list(self.tags),
            "source": self.source,
            "metadata": dict(self.metadata),
        }
