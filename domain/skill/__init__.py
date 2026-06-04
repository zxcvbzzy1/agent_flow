"""技能（Skill）子系统。

组成：
  Skill            一条可召回的技能记忆
  Embedder         向量化接缝（默认词频，后续可换 RAG embedding）
  SkillRegistry    存储 + 预计算向量
  SkillRetriever   查询 -> 相关技能（可替换为 RAG 检索器）
  loader           从 agent_flow/skills/ 的 md 文件加载

两层召回：
  1) 系统检索召回：AgentBase.start() 里 prepare_start 之后自动召回 top-k 注入；
  2) 工具召回：recall_skill 工具，由智能体按需调用。
两者共用 default_retriever，并经 runtime_hooks 暴露给 domain 层（不反向依赖 infra）。
"""

from __future__ import annotations

from domain.skill.loader import (
    DEFAULT_SKILLS_DIR,
    load_skill_file,
    load_skills_from_dir,
)
from domain.skill.registry import SkillRegistry
from domain.skill.retriever import (
    BaseSkillRetriever,
    SkillHit,
    VectorSkillRetriever,
)
from domain.skill.skill import Skill
from domain.skill.vectorizer import (
    BagOfWordsEmbedder,
    Embedder,
    cosine_similarity,
)

# 进程内默认单例：注册表 + 检索器（后续可整体替换为 RAG 版本）
default_registry = SkillRegistry()
default_retriever = VectorSkillRetriever(default_registry)

_bootstrapped = False


def bootstrap_skills(skills_dir=None, *, force: bool = False) -> VectorSkillRetriever:
    """加载技能文件并把 default_retriever 注册到 runtime_hooks（幂等）。

    在工具加载阶段调用一次即可（见 application/services/tools.py）。无技能目录时
    也会正常注册一个空检索器，系统召回因此退化为“无召回”，不影响主流程。
    """
    global _bootstrapped
    if _bootstrapped and not force:
        return default_retriever

    from domain.runtime_hooks import register_skill_retriever

    try:
        skills = load_skills_from_dir(skills_dir)
        default_registry.clear()
        default_registry.add_many(skills)
        print(f"[skill] 已加载 {len(default_registry)} 条技能（来源 {skills_dir or DEFAULT_SKILLS_DIR}）")
    except Exception as exc:  # noqa: BLE001
        print(f"[skill] 加载技能失败: {exc}")

    register_skill_retriever(default_retriever)
    _bootstrapped = True
    return default_retriever


__all__ = [
    "Skill",
    "Embedder",
    "BagOfWordsEmbedder",
    "cosine_similarity",
    "SkillRegistry",
    "BaseSkillRetriever",
    "VectorSkillRetriever",
    "SkillHit",
    "load_skill_file",
    "load_skills_from_dir",
    "DEFAULT_SKILLS_DIR",
    "default_registry",
    "default_retriever",
    "bootstrap_skills",
]
